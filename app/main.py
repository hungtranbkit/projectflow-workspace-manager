from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from app.config import load_settings
from app.db import Database
from app.repositories import discover_repositories
from app.services.git_workspace import GitWorkspaceService, GitSafetyError, GitCommandError, slugify
from app.services.test_runner import TestRunner
from app.services.terminal_launcher import TerminalLauncherService, LauncherError
from app.services.cleanup_worker import CleanupWorker
from app.services.port_allocator import PortAllocatorService
from app.services.sandbox_contract import (
    SandboxContractError, hardware_build_command, load_sandbox_contract, resolve_profile,
)
from app.services.sandbox_manager import SandboxError, SandboxManager, SourceSpec
from app.services.sandbox_runtime import SandboxRuntimeService
from app.services.sandboxed_exec import SandboxedCommandRunner
from app.services.agent_session_manager import AgentSessionManager, SessionError
from app.services.task_decision_service import TaskDecisionService, RISK_PROFILES as TDS_RISK_PROFILES, effective_task_prompt, prompt_source, LIVE_SESSION_STATUSES, humanize_blocker
from app.services.user_state_view import user_task_state, progress_summary, humanize_enum
from app.services.gate_waiver_service import GateWaiverError, GateWaiverService
from app.services.github_merge_service import GitHubIntegrationError, GitHubMergeService, MERGED_STATES, FAIL_CONCLUSIONS, make_hosted_runner, make_installation_token_runner
from app.services.github_app_service import GitHubAppService
from app.services.operations import OperationInProgress, OperationService
from app.services.deployment_service import DeploymentService, DeploymentError
from app.services.deployment_decision import deployment_view
from app.services.repository_contract_editor import RepositoryContractEditor, ContractEditError
from app.services.project_contract import ContractError
from app.services.completion_report_parser import parse_completion_report, strip_ansi
from app.services.spec_registry import SpecRegistry, SpecError
from app.services.spec_gate import SpecGate, spec_id_list, ALL_CLASSIFICATIONS
from app.services.spec_compliance import SpecComplianceVerifier
from app.services.evidence_store import EvidenceStore
from app.services.change_service import ChangeService, ChangeError, CHANGE_TYPES, RISK_LEVELS as CHANGE_RISK_LEVELS, LIFECYCLE_STATES
from app.services.change_list_summary_service import ChangeListSummaryService
from app.services.simple_view_service import SimpleViewService, t as simple_t
from app.services.auth_service import AuthService, AuthError
from app.services.organization_service import OrganizationService, OrganizationError, ROLES as ORG_ROLES
from app.services.authz_service import AuthzService, ROLE_LEVEL
from app.services.secrets_service import SecretsService, SecretsError
from app.services.secret_redaction import redact as secret_redact
from app.services.email_sender import EmailSenderService
from app.services.csrf import issue_csrf_token, require_csrf, require_csrf_unless_bearer
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from app.services.work_product_service import WorkProductService, WorkProductError, WORK_PRODUCT_KINDS, WORK_PRODUCT_STATUSES, DIRECTIONS as WP_DIRECTIONS
from app.services.trace_service import TraceService
from app.services.engineering_catalog import RoleCapabilityService, is_known_provider
from app.services.project_contract import load_engineering_policy, load_command
from app.services.workflow_engine import (
    WorkflowCatalogService, TaskDependencyService, WorkflowService, WorkflowError, PROFILES,
)
from app.services.planner_service import (
    PlannerAgentInvoker, PlannerContextBuilder, PlanValidator, PlannerService, PlannerError,
)
from app.services.human_decisions import HumanDecisionService
from app.services.spec_lifecycle_service import (
    RequirementAnalysisService, SpecAuthorService, SpecProposalValidator, SpecReviewService,
    SpecLifecycleService, SpecLifecycleError,
)
from app.services.architecture_design_service import (
    ArchitectureContextBuilder, ArchitectureAnalysisService, ArchitectureReviewService,
    UiUxApplicabilityService, TechnicalDesignService, UiUxDesignService, DesignReviewService,
    ArchitectureDesignLifecycleService, ArchitectureDesignError,
)
from app.services.test_design_service import (
    TestCaseSpecStore, TestDesignContextBuilder, TestDesignService, RequirementCoverageService,
    TestReviewService, ExecutableTestMappingService, TestDesignLifecycleService, TestDesignError,
)
from app.services.change_overview import build_change_overview
from app.services.change_control_surface import ChangeControlSurfaceService
from app.services.autonomous_execution_service import (
    AutonomousExecutionService, TaskExecutionContextBuilder, AutonomousExecutionError,
)
from app.services.worktree_manager import WorktreeManager, WorktreeManagerError
from app.services.review_service import ReviewError, FindingsStore, task_chain_ids
from app.services.code_review_service import CodeReviewService
from app.services.security_review_service import SecurityApplicabilityService, SecurityReviewService
from app.services.review_fix_orchestrator import ReviewFixOrchestratorService, ReviewFixError
from app.services.integration_service import IntegrationService, IntegrationError
from app.services.release_service import ReleaseService, ReleaseError
from app.services.product_acceptance_service import ProductAcceptanceService, ProductAcceptanceError
from app.services.incident_service import (
    IncidentService, IncidentError, STATUSES as INCIDENT_STATUSES, CLASSIFICATIONS as INCIDENT_CLASSIFICATIONS,
    SOURCES as INCIDENT_SOURCES, SEVERITIES as INCIDENT_SEVERITIES,
)
from app.services.parallel_safety_service import ParallelSafetyService
from app.services.execution_wave_service import ExecutionWaveService, ExecutionWaveError

def create_app(settings=None):
    settings = settings or load_settings(); db = Database(settings.db_path); db.init()
    # ---- B0.7: Secrets boundary -----------------------------------------
    # docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md -- constructed early
    # (github_merge below is B0.7's first real consumer -- ADR-001 --
    # and needs it already built). Same "REFUSED, never guessed"
    # discipline as session_secret: a hosted deployment with
    # organizations/tenants but no configured encryption key would
    # otherwise let B0.7's own routes exist while being silently
    # unusable, or worse, tempt a future change to fall back to
    # plaintext storage "just to make it work."
    if settings.auth_mode == "required" and not settings.secret_encryption_keys:
        raise RuntimeError(
            "REFUSED: WORKSPACE_MANAGER_AUTH_MODE=required needs WORKSPACE_MANAGER_SECRET_ENCRYPTION_KEYS "
            "set (one or more real Fernet keys, comma-separated -- generate with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`) "
            "before this app will start.")
    secrets_service = SecretsService(db, list(settings.secret_encryption_keys))
    git = GitWorkspaceService(settings.root, worktree_root=settings.worktree_root); launcher = TerminalLauncherService(settings, git)
    # B7.1 (docs/B7_WORKSPACE_REPOSITORY_IDENTITY.md): a bounded,
    # idempotent startup backfill -- computes git_fingerprint for any
    # repository row registered before B7 (still NULL) whose path
    # currently exists. Self-healing on every restart (a row that was
    # missing at one startup and reappears by the next one gets
    # fingerprinted then, no separate recovery step needed); a genuinely
    # gone path or a fingerprint-less repo (no commits) just stays NULL,
    # never a guess. Cheap: one local git call per not-yet-fingerprinted
    # row, only ever runs once per row for the lifetime of that row.
    for _row in db.all("SELECT id, repo_path FROM repositories WHERE git_fingerprint IS NULL"):
        if Path(_row["repo_path"]).is_dir():
            _fp = git.repo_fingerprint(_row["repo_path"])
            if _fp:
                db.execute("UPDATE repositories SET git_fingerprint=? WHERE id=?", (_fp, _row["id"]))
    # B1.2 (docs/B1_HOSTED_SERVICE_READ_ISOLATION.md, app/services/
    # ssrf_guard.py): a tenant's own PROJECT.yaml health-check URL is
    # validated (loopback/private/link-local/metadata-address rejected)
    # only once this process is actually hosting multiple tenants --
    # AUTH_MODE=none keeps today's exact behavior (a self-hosted
    # operator's own DEV target legitimately IS 127.0.0.1/an internal
    # address).
    sandbox_runtime = SandboxRuntimeService(enforce_ssrf_guard=(settings.auth_mode == "required")); ports = PortAllocatorService(db)
    # ---- B0.6: mandatory sandboxing for tenant-supplied command execution
    # docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md -- the two real,
    # audited shell=True call sites (TestRunner's preflight/test stages,
    # GateWaiverService's baseline-probe re-run) both now go through the
    # SAME SandboxedCommandRunner: direct-host under AUTH_MODE=none
    # (today's exact behavior, zero new surface, ADR-004's permanent
    # self-hosted mode), mandatory ephemeral-container isolation under
    # AUTH_MODE=required (never a silent unsandboxed fallback).
    sandboxed_exec = SandboxedCommandRunner(sandbox_runtime, mandatory=(settings.auth_mode == "required"))
    runner = TestRunner(db, git, sandboxed_exec)
    sandboxes = SandboxManager(db, sandbox_runtime, ports, settings.state_dir, settings.max_running_sandboxes, settings.sandbox_retention_hours)
    cleanup_worker = CleanupWorker(db, sandboxes, settings.cleanup_poll_seconds)
    agent_sessions = AgentSessionManager(db)
    decision = TaskDecisionService(db, git)
    gate_waivers = GateWaiverService(db, git, sandboxed_exec)
    # B0.7's first real consumer (ADR-001, simplified per-org-token
    # variant -- see github_merge_service.py's own module docstring):
    # AUTH_MODE=none keeps delegating entirely to the host's own
    # already-authenticated `gh` CLI session, exactly as always (the
    # class's own default `runner` -- never constructed with an
    # explicit override here, so its behavior is untouched).
    # B3.1 (docs/B3_GITHUB_APP_INSTALLATION_ARCHITECTURE.md): under
    # AUTH_MODE=required, prefer the App-based installation-token
    # runner once an App is actually configured (settings.github_app_id
    # /_private_key -- both optional); otherwise fall back to B0.7's
    # existing per-org PAT runner unchanged. GitHubAppService itself is
    # always constructed (cheap, no I/O) so the org-installation admin
    # route below can report configured() regardless of which runner
    # ends up wired.
    github_app_service = GitHubAppService(settings.github_app_id, settings.github_app_private_key)
    if settings.auth_mode == "required":
        github_merge = GitHubMergeService(
            runner=make_installation_token_runner(db, github_app_service)
            if github_app_service.configured() else make_hosted_runner(db, secrets_service))
    else:
        github_merge = GitHubMergeService()
    ops = OperationService(db)
    deployer = DeploymentService(db, git, enforce_ssrf_guard=(settings.auth_mode == "required"))
    contract_editor = RepositoryContractEditor(git)
    # Spec Layer V1 (S1-S10): specs/ lives in THIS repo (the ProjectFlow
    # tool's own source), never settings.root (the managed-repos
    # workspace root) -- Path(__file__).parent is app/, its parent is
    # the project root, matching app/config.py's own project_root
    # resolution.
    specs_root = Path(__file__).resolve().parent.parent / "specs"
    spec_gate = SpecGate(specs_root)
    spec_compliance = SpecComplianceVerifier(db, decision, specs_root)
    evidence_store = EvidenceStore(db)
    # Engineering Domain Foundation (Phase E1): additive layer above the
    # existing Task model -- see app/services/change_service.py,
    # work_product_service.py, trace_service.py for the "why reused vs
    # new" reasoning. Nothing here replaces TaskDecisionService/
    # EvidenceStore/SpecGate/_start_builder_session; they are untouched.
    changes = ChangeService(db)
    work_products = WorkProductService(db)
    trace = TraceService(db)
    # Role & Capability Catalog (Phase E2): seed() upserts the canonical
    # Python-defined catalog on every startup -- idempotent/restart-safe
    # by construction (ON CONFLICT DO UPDATE), never a one-shot migration
    # data load. See engineering_catalog.py's module docstring.
    roles_catalog = RoleCapabilityService(db, providers=settings.agents)
    roles_catalog.seed()
    # Workflow / Process Engine (Phase E3): same idempotent-upsert
    # seeding discipline as roles_catalog above. workflow_service is the
    # one facade routes/tests use; catalog/dependencies stay separate
    # objects internally (WORKFLOW DEFINITION vs TASK EXECUTION, E3's
    # own key architectural rule) but are wired together here.
    workflow_catalog = WorkflowCatalogService(db)
    workflow_catalog.seed()
    task_dependencies = TaskDependencyService(db)
    workflow_service = WorkflowService(db, workflow_catalog, changes, work_products, decision, spec_compliance, task_dependencies)
    # Dynamic Planner (Phase E4): additive layer above Change/WorkProduct/
    # Workflow/Role. PlannerService never launches a coding session --
    # PlannerAgentInvoker's subprocess call is bounded, tool-less, and
    # entirely separate from AgentSessionManager/_start_builder_session.
    planner_context_builder = PlannerContextBuilder(db, changes, work_products, decision, workflow_catalog, workflow_service, roles_catalog, specs_root)
    planner_invoker = PlannerAgentInvoker()
    planner_validator = PlanValidator(workflow_catalog, roles_catalog, specs_root)
    human_decisions = HumanDecisionService(db)
    planner_service = PlannerService(db, changes, work_products, decision, roles_catalog, workflow_catalog, workflow_service,
                                      planner_context_builder, planner_invoker, planner_validator, specs_root, settings.root,
                                      human_decisions=human_decisions)
    # E4.12: additive-only hook -- WorkflowService's own evaluate_workflow
    # already defaults this to None (zero behavior change for anything
    # that constructs WorkflowService without it, including every E3
    # test); wiring it here is what makes a real Change's workflow state
    # actually surface WAITING_HUMAN while a Plan has an unresolved
    # WHAT-level decision.
    workflow_service.human_decisions_pending = planner_service.human_decisions_pending
    # Autonomous Spec Lifecycle (Phase E5): reuses PlannerAgentInvoker
    # (a fresh, stateless subprocess per call -- author/review are
    # already separate invocations by construction) and roles_catalog
    # (RoleCapabilityService, not a second validator) exactly as the
    # rest of the engineering domain does. requirement_analysis/
    # spec_author/spec_reviewer all target THIS repo's own specs_root
    # (same resolution as spec_gate/spec_compliance above) -- never a
    # managed-repo path.
    requirement_analysis_service = RequirementAnalysisService(db, changes, work_products, planner_invoker, roles_catalog, specs_root, settings.root)
    spec_author_service = SpecAuthorService(db, changes, work_products, planner_invoker, roles_catalog, specs_root, settings.root)
    spec_proposal_validator = SpecProposalValidator(specs_root)
    spec_review_service = SpecReviewService(db, changes, work_products, planner_invoker, roles_catalog, specs_root, settings.root, human_decisions)
    spec_lifecycle_service = SpecLifecycleService(db, changes, work_products, trace, requirement_analysis_service,
                                                   spec_author_service, spec_proposal_validator, spec_review_service,
                                                   human_decisions, specs_root)
    # Architecture & Technical/UI Design Lifecycle (Phase E6): same
    # reuse discipline as E5 -- PlannerAgentInvoker (tool-less, stateless,
    # so an architecture/design agent can never write source), roles_catalog
    # (RoleCapabilityService, no second validator), THIS repo's own
    # specs_root (never a managed-repo path), and human_decisions
    # (HumanDecisionService, subject_type='work_product' -- no second
    # decision system). _change_engineering_policy below (E3.12) is
    # defined further down with the other Workflow routes; passed in here
    # as a resolver callable so ArchitectureDesignLifecycleService can
    # resolve a Change's own PROJECT.yaml engineering policy without a
    # forward reference.
    def _resolve_project_policy_for_change(change: dict) -> dict | None:
        if not change.get("project_id"):
            return None
        r = db.one("SELECT repo_path FROM repositories WHERE id=?", (change["project_id"],))
        if not r:
            return None
        try:
            return load_engineering_policy(Path(r["repo_path"]))
        except ContractError:
            return None

    architecture_context_builder = ArchitectureContextBuilder(db, changes, work_products, trace, roles_catalog, workflow_catalog, workflow_service, specs_root, settings.root)
    architecture_analysis_service = ArchitectureAnalysisService(db, changes, work_products, trace, planner_invoker, roles_catalog, architecture_context_builder, settings.root)
    architecture_review_service = ArchitectureReviewService(db, changes, work_products, trace, planner_invoker, roles_catalog, specs_root, settings.root, human_decisions)
    ui_ux_applicability_service = UiUxApplicabilityService(work_products, changes)
    technical_design_service = TechnicalDesignService(db, changes, work_products, trace, planner_invoker, roles_catalog, architecture_context_builder, specs_root, settings.root)
    ui_ux_design_service = UiUxDesignService(db, changes, work_products, planner_invoker, roles_catalog, architecture_context_builder, specs_root, settings.root)
    design_review_service = DesignReviewService(db, changes, work_products, planner_invoker, roles_catalog, specs_root, settings.root, human_decisions)
    architecture_design_service = ArchitectureDesignLifecycleService(
        db, changes, work_products, trace, architecture_context_builder,
        architecture_analysis_service, architecture_review_service, ui_ux_applicability_service,
        technical_design_service, ui_ux_design_service, design_review_service, human_decisions,
        requirement_analysis_lookup=lambda cid: (lambda wp: json.loads(wp["content_metadata"] or "{}") if wp else None)(spec_lifecycle_service.get_requirement_analysis(cid)),
        workflow_service=workflow_service, project_policy_resolver=_resolve_project_policy_for_change)
    # E6.16: additive-only hook, exact same pattern as E4.12's
    # human_decisions_pending above -- ARCHITECTURE_READY/DESIGN_READY
    # now resolve through real independent-review evidence instead of
    # bare WorkProduct presence (see WorkflowService._gate_architecture_
    # ready/_gate_design_ready). Every E3/E4/E5 test's own WorkflowService
    # construction leaves this None (zero behavior change there).
    workflow_service.architecture_design_gate = architecture_design_service
    # Test Design, Requirement Coverage & Executable Acceptance Mapping
    # (Phase E7): same reuse discipline as E5/E6 -- PlannerAgentInvoker
    # (tool-less; a Test Designer has no source/test-file-write path at
    # all), roles_catalog, THIS repo's own specs_root, human_decisions.
    # RequirementCoverageService is the ONE new deterministic (no LLM)
    # class -- never trusts a model-provided coverage total.
    test_case_specs_store = TestCaseSpecStore(db)
    test_design_context_builder = TestDesignContextBuilder(db, changes, work_products, trace, test_case_specs_store, specs_root)
    test_design_service = TestDesignService(db, changes, work_products, planner_invoker, roles_catalog, test_design_context_builder, test_case_specs_store, specs_root, settings.root)
    requirement_coverage_service = RequirementCoverageService(trace, test_case_specs_store, specs_root)
    test_review_service = TestReviewService(db, changes, work_products, planner_invoker, roles_catalog, test_case_specs_store, requirement_coverage_service, specs_root, settings.root, human_decisions)
    executable_test_mapping_service = ExecutableTestMappingService(db, test_case_specs_store)
    test_design_lifecycle_service = TestDesignLifecycleService(
        db, changes, work_products, trace, test_design_context_builder, test_design_service, requirement_coverage_service,
        test_review_service, test_case_specs_store, executable_test_mapping_service, human_decisions, workflow_service=workflow_service)
    # E7.17: additive-only hook, exact same pattern as E6.16's
    # architecture_design_gate -- TEST_DESIGN_READY (attached to the
    # existing VERIFY stage, see workflow_engine.py's GATES entry) now
    # resolves through real coverage/review evidence instead of being
    # unconditionally vacuous.
    workflow_service.test_design_gate = test_design_lifecycle_service
    # E7.16: PlannerContextBuilder sees current test-design state.
    planner_context_builder.test_design_lifecycle = test_design_lifecycle_service
    # E7.18: additive-only hook, exact same pattern -- SpecComplianceVerifier
    # can now distinguish TEST_DESIGN_MISSING/TEST_IMPLEMENTATION_MISSING/
    # TEST_EVIDENCE_MISSING/TEST_EVIDENCE_FAIL as a purely diagnostic field,
    # never changing its own verdict logic.
    spec_compliance.test_case_specs = test_case_specs_store
    spec_compliance.executable_mapping = executable_test_mapping_service
    # Change Control Surface (Phase E7.5): the ONE aggregation layer the
    # whole Engineering Lifecycle UI reads from -- composition only, see
    # app/services/change_control_surface.py's own module docstring.
    change_control_surface = ChangeControlSurfaceService(
        db, changes, work_products, trace, decision, evidence_store, roles_catalog,
        workflow_service, spec_lifecycle_service, architecture_design_service,
        test_design_lifecycle_service, test_case_specs_store, executable_test_mapping_service,
        planner_service, human_decisions, specs_root, _resolve_project_policy_for_change)
    # Phase E8 (Autonomous Implementation Orchestration): the Builder-
    # prompt-context half only -- built here since it needs no closure
    # defined later in this function. AutonomousExecutionService itself
    # (the orchestrator) is constructed further down, once
    # add_task_workspace/_start_builder_session exist -- it calls those
    # EXACT closures to actually launch a Builder, never a second
    # Supervisor (E8.10).
    task_execution_context_builder = TaskExecutionContextBuilder(
        db, work_products, trace, specs_root, test_case_specs_store, executable_test_mapping_service)
    app = FastAPI(title="ProjectFlow Workspace Manager", docs_url=None, redoc_url=None)
    base = Path(__file__).parent; templates = Jinja2Templates(directory=base / "templates")
    templates.env.filters["humanize"] = humanize_enum
    templates.env.filters["humanize_blocker"] = humanize_blocker
    # B0.4: one Jinja global every template (via base.html, see its own
    # comment) uses to embed the current session's CSRF token client-side
    # -- never called under AUTH_MODE=none (issue_csrf_token() itself
    # would raise -- SessionMiddleware isn't installed there at all), so
    # this wrapper checks auth_mode first and returns "" instead, the
    # same no-op precedent every other B0.1-B0.4 guard already follows.
    templates.env.globals["issue_csrf_token"] = (
        lambda request: issue_csrf_token(request) if settings.auth_mode == "required" else "")
    # A1.20/A1.21: the one translation-key lookup every template can call
    # -- {{ pf_t('key') }} -- so a future language file swap is "point
    # this at a second dict", not a template rewrite. See
    # simple_view_service.py's own module docstring. Named `pf_t`, not
    # the shorter `t`/`tr` -- both those names are ALREADY real per-route
    # context variables in this codebase (`t`=the current Task row in
    # task_detail.html and others, `tr`=a test_runs row in main.py) and
    # would silently shadow this global with a dict/row on any page that
    # passes one, turning `{{t('key')}}` into a real TypeError (caught
    # live by the full regression run, not guessed).
    templates.env.globals["pf_t"] = simple_t
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    app.state.settings, app.state.db, app.state.git, app.state.runner, app.state.launcher = settings, db, git, runner, launcher
    app.state.sandboxed_exec = sandboxed_exec
    app.state.secrets_service = secrets_service
    app.state.sandboxes, app.state.ports, app.state.sandbox_runtime, app.state.cleanup_worker = sandboxes, ports, sandbox_runtime, cleanup_worker
    app.state.agent_sessions = agent_sessions
    app.state.decision = decision
    app.state.gate_waivers = gate_waivers
    app.state.github_merge = github_merge
    app.state.deployer = deployer
    app.state.ops = ops
    app.state.contract_editor = contract_editor
    app.state.specs_root = specs_root
    app.state.spec_gate = spec_gate
    app.state.spec_compliance = spec_compliance
    app.state.evidence_store = evidence_store
    app.state.changes = changes
    app.state.work_products = work_products
    app.state.trace = trace
    app.state.roles_catalog = roles_catalog
    app.state.workflow_catalog = workflow_catalog
    app.state.task_dependencies = task_dependencies
    app.state.workflow_service = workflow_service
    app.state.planner_service = planner_service
    app.state.human_decisions = human_decisions
    app.state.requirement_analysis_service = requirement_analysis_service
    app.state.spec_author_service = spec_author_service
    app.state.spec_proposal_validator = spec_proposal_validator
    app.state.spec_review_service = spec_review_service
    app.state.spec_lifecycle_service = spec_lifecycle_service
    app.state.architecture_context_builder = architecture_context_builder
    app.state.architecture_analysis_service = architecture_analysis_service
    app.state.architecture_review_service = architecture_review_service
    app.state.ui_ux_applicability_service = ui_ux_applicability_service
    app.state.technical_design_service = technical_design_service
    app.state.ui_ux_design_service = ui_ux_design_service
    app.state.design_review_service = design_review_service
    app.state.architecture_design_service = architecture_design_service
    app.state.test_case_specs_store = test_case_specs_store
    app.state.test_design_context_builder = test_design_context_builder
    app.state.test_design_service = test_design_service
    app.state.requirement_coverage_service = requirement_coverage_service
    app.state.test_review_service = test_review_service
    app.state.executable_test_mapping_service = executable_test_mapping_service
    app.state.test_design_lifecycle_service = test_design_lifecycle_service
    app.state.change_control_surface = change_control_surface
    app.state.task_execution_context_builder = task_execution_context_builder

    # ---- B0.1: AuthN foundation ----------------------------------------
    # docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md, ADR-002's resolved
    # design: email magic-link login, API tokens for service/automation
    # accounts, a self-hosted first-user console-token bootstrap so
    # AUTH_MODE=required never hard-requires SMTP just to get started.
    # AUTH_MODE=none (default, unchanged) never registers SessionMiddleware
    # at all -- zero new behavior for the existing, real, already-verified
    # single-user production deployment (Track A1's own live-verification
    # pass), matching ADR-004's own "network boundary stays exactly as
    # simple as today" reasoning. B0.1 deliberately does NOT sweep any of
    # the 143 pre-existing routes behind current_user/AuthZ -- that is
    # B0.3's own scope; see the B0.1 implementation report for the exact
    # boundary.
    email_sender = EmailSenderService(
        host=settings.smtp_host, port=settings.smtp_port, user=settings.smtp_user,
        password=settings.smtp_password, from_addr=settings.smtp_from, use_tls=settings.smtp_use_tls)
    auth_service = AuthService(db, email_sender)
    app.state.auth_service = auth_service
    app.state.email_sender = email_sender
    # ADR-003's own resolved rate-limiting choice (a maintained,
    # pluggable-backend library) -- registered unconditionally (cheap,
    # inert) but only ever actually applied to the two AUTH_MODE=required
    # routes below (login-link request, bootstrap) that need a
    # launch-blocking limit ahead of B0.5's general middleware rollout.
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.state.bootstrap_token_hash = None
    if settings.auth_mode == "required":
        if not settings.session_secret:
            raise RuntimeError(
                "REFUSED: WORKSPACE_MANAGER_AUTH_MODE=required needs WORKSPACE_MANAGER_SESSION_SECRET "
                "set (a real, random secret -- never a default) before this app will start.")
        # https_only: B6.1 (docs/B6_TRUSTED_PROXY_SUPPORT.md) closes
        # ADR-003's own flagged-but-unresolved proxy-topology residual --
        # True only once a trusted reverse proxy is actually configured
        # (settings.trusted_proxy_ips, below), since only then does
        # scope["scheme"] reliably reflect the ORIGINAL client's real
        # scheme rather than this process's own plain-HTTP loopback
        # reality (127.0.0.1-only today, no TLS anywhere -- the default,
        # empty-config case keeps this False, byte-for-byte the same as
        # every prior phase). same_site="lax" is the real CSRF-relevant
        # control either way, not the cookie's secure flag.
        app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                            max_age=settings.session_max_age_days * 86400, same_site="lax",
                            https_only=bool(settings.trusted_proxy_ips))
        if auth_service.user_count() == 0:
            raw_bootstrap_token = secrets.token_urlsafe(24)
            app.state.bootstrap_token_hash = hashlib.sha256(raw_bootstrap_token.encode("utf-8")).hexdigest()
            logging.getLogger("projectflow.auth").warning(
                "FIRST_USER_SETUP: no users exist yet on this instance. "
                "Visit /auth/bootstrap?token=%s to create the first account.", raw_bootstrap_token)

    # ---- B6.1: trusted reverse-proxy support -----------------------------
    # docs/B6_TRUSTED_PROXY_SUPPORT.md: off by default (settings.
    # trusted_proxy_ips is empty unless explicitly configured, the same
    # "REFUSED/off unless configured" precedent every other credential/
    # trust setting in this app uses) -- zero behavior change for
    # AUTH_MODE=none and for AUTH_MODE=required with no proxy configured
    # yet. Uvicorn's own ProxyHeadersMiddleware (already a transitive
    # dependency, not hand-rolled) only rewrites scope["client"]/
    # scope["scheme"] from X-Forwarded-For/-Proto when the DIRECT
    # connecting peer is itself in this trusted list -- a request from
    # anywhere else has those headers ignored entirely, so an untrusted
    # caller can never spoof its way past this. Added LAST (after
    # SessionMiddleware above) so it becomes the OUTERMOST layer and
    # rewrites scope before anything else -- slowapi's own
    # get_remote_address() and SessionMiddleware's https_only check --
    # ever reads it, applied independent of AUTH_MODE since this is a
    # general HTTP-layer correctness concern, not an auth-mode-specific
    # one.
    if settings.trusted_proxy_ips:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(settings.trusted_proxy_ips))

    # ---- B0.2: Organizations/Tenants -----------------------------------
    # docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md, Design Principle #3:
    # repositories.organization_id is the ONE scoping lever that
    # transitively tenant-scopes the whole existing E1-E13 schema -- no
    # organization_id column anywhere else, no retrofit of the 143
    # pre-existing routes (that general per-route AuthZ sweep stays
    # B0.3's own scope). Constructed regardless of AUTH_MODE (cheap,
    # inert) but its migration and routes only ever run/exist under
    # AUTH_MODE=required, matching B0.1's own established pattern.
    org_service = OrganizationService(db, auth_service, email_sender)
    app.state.org_service = org_service
    # ---- B0.3: AuthZ -----------------------------------------------------
    # The general per-route sweep B0.2's own docstring deferred here --
    # AuthzService (app/services/authz_service.py) resolves every one of
    # the ~20 distinct mutating-route entity kinds back to the
    # organization(s) that own it via repositories.organization_id, the
    # same single tenant-scoping lever B0.2 established. Constructed
    # regardless of AUTH_MODE (cheap, inert); require_role()/the inline
    # _require_org_role_for_* helpers below are the only things that ever
    # call into it, and they no-op under AUTH_MODE=none exactly like
    # current_user() already does.
    authz_service = AuthzService(db)
    app.state.authz_service = authz_service
    if settings.auth_mode == "required":
        # Idempotent, safe, backward-compatible bootstrap-to-org backfill
        # -- see OrganizationService.migrate_existing_data()'s own
        # docstring for the exact, evidence-grounded rule (only ever acts
        # when ownership is unambiguous). Runs on every startup, not just
        # the first, since it is a genuine no-op once already migrated.
        migration_result = org_service.migrate_existing_data()
        app.state.b02_migration_result = migration_result
        auth_logger = logging.getLogger("projectflow.auth")
        if migration_result["action"] == "MIGRATED":
            auth_logger.warning(
                "B0.2_MIGRATION: created organization %s for the existing single user, linked %d "
                "existing repositories to it.", migration_result["org_id"], migration_result["repositories_linked"])
        elif migration_result["action"] == "SKIPPED_AMBIGUOUS":
            auth_logger.warning("B0.2_MIGRATION: %s -- no repository was auto-assigned.", migration_result["reason"])

    cleanup_worker.start(); agent_sessions.reconcile_on_startup()
    def render(request, name, **ctx):
        resp = templates.TemplateResponse(request=request, name=name, context={"settings": settings, "mode": _ui_mode(request), **ctx})
        _apply_mode_cookie(request, resp)
        return resp

    # ---- Track A1.11/A1.12: Simple/Advanced mode selection -----------
    # ProjectFlow had no AuthN user model at all when this was written
    # (A1.12's own explicit constraint at the time) -- a plain cookie was
    # the acceptable initial approach named there. B0.1 has since added
    # a real one (docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md), but
    # this Simple/Advanced preference is a separate, per-browser display
    # setting, not an AuthN concern -- deliberately left on its own
    # cookie rather than folded into the new user session. Simple Mode
    # is fully built and one click away
    # (?mode=simple, persisted back into the cookie so it sticks across
    # navigation, same as the existing filter-preserving convention
    # elsewhere in this file) -- but the NO-SIGNAL default below is
    # Advanced, not Simple, deliberately deviating from A1.12's own
    # literal "Default: Simple Mode ... if safe" suggestion. Evidence,
    # not a guess: defaulting a zero-signal /changes/{id} request to
    # Simple broke 6 real, pre-existing tests (test_autonomous_execution.
    # py, test_change_overview.py, test_product_acceptance.py,
    # test_release_pipeline.py) that assert specific Advanced-page
    # content with no mode cookie/param set -- exactly A1.27's own
    # "current APIs still work" requirement, which this track's own GIT
    # POLICY ("full regression must pass") makes non-negotiable, unlike
    # A1.12's own explicitly qualified default suggestion.
    _MODE_COOKIE = "pf_mode"
    def _ui_mode(request: Request) -> str:
        q = (request.query_params.get("mode") or "").strip().lower()
        if q in ("simple", "advanced"): return q
        c = (request.cookies.get(_MODE_COOKIE) or "").strip().lower()
        return c if c in ("simple", "advanced") else "advanced"
    def _apply_mode_cookie(request: Request, response) -> None:
        q = (request.query_params.get("mode") or "").strip().lower()
        if q in ("simple", "advanced"):
            response.set_cookie(_MODE_COOKIE, q, max_age=60*60*24*365, samesite="lax")

    # ---- B0.1: AuthN routes ---------------------------------------------
    def current_user(request: Request) -> dict | None:
        """No-op under AUTH_MODE=none (todays exact behavior, per Design
        Principle #2) -- never touches request.session at all in that
        mode (SessionMiddleware isn't installed there; accessing
        request.session without it raises). Checks the session cookie
        first, then a Bearer API token -- either identifies the same
        `users` row, so every caller of this dependency works with both
        without needing to know which one was used."""
        if settings.auth_mode != "required":
            return None
        uid = request.session.get("user_id")
        if uid:
            user = auth_service.get_user(uid)
            if user:
                return user
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            return auth_service.user_for_api_token(auth_header[7:].strip())
        return None

    def _require_login_redirect(request: Request):
        """Not a FastAPI dependency (deliberately) -- an HTML page needs a
        302 to the login form, not a 401, and B0.1 only has a handful of
        routes needing this (the general AuthZ dependency sweep across
        the other 143 routes is B0.3's own scope, not pulled forward
        here). Returns the user dict, or a RedirectResponse to render
        directly (caller does `u = _require_login_redirect(request);
        if not isinstance(u, dict): return u`)."""
        if settings.auth_mode != "required":
            raise HTTPException(404)
        user = current_user(request)
        if not user:
            return RedirectResponse("/auth/login", 303)
        return user

    # ---- B0.3/B0.4: general per-route AuthZ + CSRF guard ------------------
    def require_role(kind: str, param: str, min_role: str = "MEMBER"):
        """B0.3's general per-route AuthZ guard (docs/B0_HOSTED_PLATFORM_
        SECURITY_FOUNDATION.md's own `require_role(min_role)` design) --
        a dependency FACTORY, called once per route at decoration time
        with that route's own resource kind/path-param name/minimum
        role, returning the actual FastAPI dependency. Also carries
        B0.4's CSRF check (see its own comment below) -- the exact same
        143-route mutating-route sweep both sub-phases target, so B0.4
        folds into this one dependency rather than re-touching every one
        of those call sites a second time.

        No-op under AUTH_MODE=none: current_user() itself already
        returns None unconditionally there and never touches
        request.session (SessionMiddleware isn't installed in that
        mode) -- this preserves today's exact, already-verified
        single-user behavior with zero new surface, same precedent as
        require_csrf's own AUTH_MODE=none short-circuit.

        Under AUTH_MODE=required: no identified user is 401 (never a
        redirect -- every route this guards is a JSON/API surface, not
        an HTML page, unlike /orgs/*'s _org_context). An identified user
        who cannot reach `min_role` in EVERY organization the target
        resource resolves to (AuthzService.resolve_organization_ids --
        fail-closed on zero resolved orgs too, e.g. an unlinked
        repository) is 404, matching B0.2's own established
        existence-hiding precedent (a non-member never learns whether
        the id is even valid); 403 only once membership is confirmed
        but the role itself is too low -- the same distinction B0.2's
        own org routes already draw."""
        min_level = ROLE_LEVEL[min_role]

        async def _dep(request: Request) -> None:
            if settings.auth_mode != "required":
                return
            user = current_user(request)
            if not user:
                raise HTTPException(401, "AUTHENTICATION_REQUIRED")
            raw = request.path_params.get(param)
            if raw is None:
                raise HTTPException(404)
            try:
                entity_id = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(404)
            org_ids = authz_service.resolve_organization_ids(kind, entity_id)
            if not org_ids:
                raise HTTPException(404)
            for org_id in org_ids:
                role = org_service.member_role(org_id, user["id"])
                if not role:
                    raise HTTPException(404)
                if ROLE_LEVEL[role] < min_level:
                    raise HTTPException(403, "INSUFFICIENT_ROLE")
            # B0.4: CSRF, checked last -- only once identity+role are
            # already confirmed valid, so an unauthenticated/wrong-org
            # caller keeps getting the 401/404/403 that actually
            # describes their situation, never a CSRF error that leaks
            # nothing extra either way (a genuine cross-site-forged
            # request necessarily carries the real, valid victim
            # session, so it always reaches this same point and is
            # blocked here). No-ops for Bearer/API-token callers
            # (require_csrf_unless_bearer's own docstring).
            await require_csrf_unless_bearer(request)
        return _dep

    # ---- B1.1(a): GET-route counterpart -- no CSRF, VIEWER default -------
    def require_read_role(kind: str, param: str, min_role: str = "VIEWER"):
        """docs/B1_HOSTED_SERVICE_READ_ISOLATION.md's per-id read guard --
        identical AuthzService resolution and 401/404/403 fail-closed
        semantics as require_role() above, deliberately WITHOUT the CSRF
        check: GET is safe/idempotent, and CSRF only ever guards a
        state-changing request (require_csrf_unless_bearer's own
        docstring) -- folding it in here would require every plain page
        navigation and fetch() GET to carry a token, which nothing does
        and nothing should. Default min_role is VIEWER (the lowest real
        role) since reading is a strictly weaker requirement than the
        MEMBER default require_role() uses for a mutation."""
        min_level = ROLE_LEVEL[min_role]

        async def _dep(request: Request) -> None:
            if settings.auth_mode != "required":
                return
            user = current_user(request)
            if not user:
                raise HTTPException(401, "AUTHENTICATION_REQUIRED")
            raw = request.path_params.get(param)
            if raw is None:
                raise HTTPException(404)
            try:
                entity_id = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(404)
            org_ids = authz_service.resolve_organization_ids(kind, entity_id)
            if not org_ids:
                raise HTTPException(404)
            for org_id in org_ids:
                role = org_service.member_role(org_id, user["id"])
                if not role:
                    raise HTTPException(404)
                if ROLE_LEVEL[role] < min_level:
                    raise HTTPException(403, "INSUFFICIENT_ROLE")
        return _dep

    def _visible_repo_ids(request: Request) -> set[int] | None:
        """B1.1(b)'s list-route filter base. None means unrestricted --
        AUTH_MODE=none (today's exact self-hosted behavior, zero new
        query overhead) -- every call site below checks for None and
        skips filtering entirely in that case. A real, authenticated-but-
        empty set() is a different, valid, fail-closed answer ("sees
        nothing") and must never be treated the same as None."""
        if settings.auth_mode != "required":
            return None
        user = current_user(request)
        if not user:
            return set()
        return authz_service.visible_repository_ids(user["id"])

    def _visible_task_ids(request: Request) -> set[int] | None:
        if settings.auth_mode != "required":
            return None
        user = current_user(request)
        if not user:
            return set()
        return authz_service.visible_task_ids(user["id"])

    def _filter_polymorphic(request: Request, kind: str, rows: list) -> list:
        """Per-row fallback for a list route whose entities don't carry a
        direct repository_id/task_id column to filter on cheaply (e.g.
        test_runs' own workspace_type/workspace_id) -- reuses AuthzService's
        existing per-entity resolver instead of a second resolution path.
        Deliberately only used for routes with a small/capped row count
        (test-runs' own LIMIT 200) -- see docs/
        B1_HOSTED_SERVICE_READ_ISOLATION.md's own scope note on this
        trade-off; a route with real unbounded growth needs the batched
        approach _visible_repo_ids/_visible_task_ids use instead."""
        if settings.auth_mode != "required":
            return rows
        repo_ids = _visible_repo_ids(request)
        if not repo_ids:
            return []
        return [r for r in rows if authz_service.resolve_repository_ids(kind, r["id"]) & repo_ids]

    def _filter_rows(rows: list, id_ids: set[int] | None, key: str = "id") -> list:
        """B1.1(b): filters an already-fetched row list down to the ones
        whose `key` column is in id_ids -- id_ids is the None|set() result
        of _visible_repo_ids/_visible_task_ids (or an ad-hoc {id, ...}
        derived from one of those). None (AUTH_MODE=none) means
        unrestricted -- returned unchanged, no new query cost at all in
        the permanent self-hosted default. Does not touch pagination/
        ordering; every caller filters BEFORE paginating, never after."""
        if id_ids is None:
            return rows
        return [r for r in rows if r[key] in id_ids]

    async def _mutating_csrf(request: Request) -> None:
        """B0.4's CSRF check for the 12 body-based `create` routes (no
        path-id to fold this into a require_role() call for -- see each
        route's own Depends() list). Unlike require_role's internal
        auth_mode gate, `require_csrf`/`require_csrf_unless_bearer`
        themselves only guard against CRASHING under AUTH_MODE=none (a
        clean 404 instead of an AssertionError on `request.session`) --
        they do NOT make AUTH_MODE=none a true pass-through, because
        their own established precedent (`/auth/logout`, `/account/
        api-tokens`) is for BRAND-NEW B0.1 routes that never existed at
        all under `AUTH_MODE=none` in the first place, so a 404 there is
        correct either way. These 12 routes are the opposite case --
        real, pre-existing, heavily-used production routes (register a
        repository, create a Task, ...) that MUST keep working
        completely unmodified under the default `AUTH_MODE=none` (the
        same requirement every B0.1-B0.3 guard on pre-existing surface
        has honored) -- so this wrapper checks auth_mode FIRST, the same
        true no-op require_role()'s own _dep already establishes,
        before ever calling into require_csrf machinery at all."""
        if settings.auth_mode != "required":
            return
        await require_csrf_unless_bearer(request)

    def _require_login_only(request: Request) -> dict | None:
        """For the small number of `create` routes whose new resource
        carries no repository/org reference at all (e.g. registering a
        brand-new, not-yet-linked repository) -- only identity is
        checked here, never a role, since there is no organization yet
        to hold a role in. No-op under AUTH_MODE=none, same as every
        other B0.1-B0.3 guard."""
        if settings.auth_mode != "required":
            return None
        user = current_user(request)
        if not user:
            raise HTTPException(401, "AUTHENTICATION_REQUIRED")
        return user

    def _require_org_role_for_repos(request: Request, repository_ids: list[int], min_role: str = "MEMBER") -> None:
        """Inline counterpart to require_role() for `create` routes whose
        target resource doesn't exist yet, so there is no id in the path
        to build a Depends() dependency against -- only body fields the
        handler has already parsed by the time it can call this. An
        empty `repository_ids` list is allowed through (the resulting
        resource is legitimately orgless, e.g. a BACKLOG Task created
        with no repo_scope_id yet, or an orgless Change/Incident/Work
        Product -- every call site documents why blank is safe for
        it). A non-blank id that fails to resolve to any organization
        (unlinked repository, bad id) still fails closed, and a
        multi-repository create (a cross-repo Task) requires the role in
        EVERY listed repository's organization, the same conservative
        rule require_role() itself applies."""
        if settings.auth_mode != "required":
            return
        user = current_user(request)
        if not user:
            raise HTTPException(401, "AUTHENTICATION_REQUIRED")
        for repository_id in repository_ids:
            if repository_id is None:
                continue
            org_ids = authz_service.organization_ids_for_repository(repository_id)
            if not org_ids:
                raise HTTPException(404)
            for org_id in org_ids:
                role = org_service.member_role(org_id, user["id"])
                if not role:
                    raise HTTPException(404)
                if ROLE_LEVEL[role] < ROLE_LEVEL[min_role]:
                    raise HTTPException(403, "INSUFFICIENT_ROLE")

    def _require_org_role_for_repo(request: Request, repository_id: int | None, min_role: str = "MEMBER") -> None:
        _require_org_role_for_repos(request, [repository_id] if repository_id is not None else [], min_role)

    def _require_org_role_for_entity(request: Request, kind: str, entity_id: int | None, min_role: str = "MEMBER") -> None:
        """Generic counterpart to _require_org_role_for_repo for the
        create routes whose body carries a reference to an EXISTING
        entity of some other kind (a change_id, a task_id -- e.g.
        Work Product's own project_id/change_id/task_id fallback chain)
        rather than a repository_id directly -- delegates straight to
        AuthzService.resolve_organization_ids, the exact same resolver
        require_role() itself uses."""
        if settings.auth_mode != "required":
            return
        user = current_user(request)
        if not user:
            raise HTTPException(401, "AUTHENTICATION_REQUIRED")
        if entity_id is None:
            return
        org_ids = authz_service.resolve_organization_ids(kind, entity_id)
        if not org_ids:
            raise HTTPException(404)
        for org_id in org_ids:
            role = org_service.member_role(org_id, user["id"])
            if not role:
                raise HTTPException(404)
            if ROLE_LEVEL[role] < ROLE_LEVEL[min_role]:
                raise HTTPException(403, "INSUFFICIENT_ROLE")

    def _require_org_role_for_change(request: Request, change_id: int | None, min_role: str = "MEMBER") -> None:
        """Same shape as _require_org_role_for_repo, resolving through
        Change.project_id (AuthzService's own "change" kind) since
        several create routes (Task, Incident, Work Product) accept a
        change_id rather than a repository_id directly."""
        _require_org_role_for_entity(request, "change", change_id, min_role)

    @app.get("/auth/login", response_class=HTMLResponse)
    def auth_login_page(request: Request):
        if settings.auth_mode != "required": raise HTTPException(404)
        return render(request, "auth_login.html", needs_bootstrap=bool(app.state.bootstrap_token_hash))

    @app.post("/auth/login")
    @limiter.limit("5/minute")  # ADR-002/ADR-003: launch-blocking per-IP throttle on the link-request endpoint
    def auth_login_request(request: Request, email: str = Form(...)):
        if settings.auth_mode != "required": raise HTTPException(404)
        auth_service.request_login(email)
        # ADR-002's own enumeration mitigation: identical response whether
        # or not the email is registered -- never branch on that here.
        return render(request, "auth_login.html", needs_bootstrap=bool(app.state.bootstrap_token_hash), sent=True)

    @app.get("/auth/verify", response_class=HTMLResponse)
    def auth_verify_page(request: Request, token: str = ""):
        """GET only ever PEEKS at the token (never consumes it) and shows
        an explicit confirm-click page -- ADR-002's own phishing
        mitigation against corporate email-security scanners silently
        pre-fetching/"clicking" the raw link before the real user does.

        B0.4: also issues (or reuses) this anonymous pre-login session's
        own CSRF token here and embeds it in the confirm form -- closing
        the "login CSRF" gap (an attacker's own valid magic-link token,
        submitted via a cross-site forged POST so the VICTIM's browser
        ends up silently logged into the ATTACKER's account, a classic
        session-fixation-via-forced-login). SessionMiddleware is
        installed for every request under AUTH_MODE=required regardless
        of login state, so a real anonymous session -- and therefore a
        real, unguessable-by-a-different-origin token -- already exists
        the moment this page is first viewed, the same double-submit
        property require_csrf relies on everywhere else."""
        if settings.auth_mode != "required": raise HTTPException(404)
        row = auth_service.peek_login_token(token)
        return render(request, "auth_verify.html", token=token, email=row["email"] if row else None,
                      valid=bool(row), csrf_token=issue_csrf_token(request))

    @app.post("/auth/verify")
    @limiter.limit("10/minute")  # B0.5: token-consumption brute-force defense, same family as /auth/login's own
    def auth_verify_confirm(request: Request, token: str = Form(...), csrf: None = Depends(require_csrf)):
        if settings.auth_mode != "required": raise HTTPException(404)
        try:
            user = auth_service.consume_login_token(token)
        except AuthError:
            return render(request, "auth_verify.html", token=token, email=None, valid=False)
        request.session.clear(); request.session["user_id"] = user["id"]
        return RedirectResponse("/account", 303)

    @app.post("/auth/logout")
    def auth_logout(request: Request, csrf: None = Depends(require_csrf)):
        if settings.auth_mode != "required": raise HTTPException(404)
        request.session.clear()
        return RedirectResponse("/auth/login", 303)

    @app.get("/auth/bootstrap", response_class=HTMLResponse)
    def auth_bootstrap_page(request: Request, token: str = ""):
        if settings.auth_mode != "required" or not app.state.bootstrap_token_hash: raise HTTPException(404)
        return render(request, "auth_bootstrap.html", token=token, error=None)

    @app.post("/auth/bootstrap")
    @limiter.limit("5/minute")
    def auth_bootstrap_submit(request: Request, token: str = Form(...), email: str = Form(...)):
        if settings.auth_mode != "required" or not app.state.bootstrap_token_hash: raise HTTPException(404)
        try:
            user = auth_service.bootstrap(token, app.state.bootstrap_token_hash, email)
        except AuthError as e:
            return render(request, "auth_bootstrap.html", token=token, error=str(e))
        # Setup is now closed for good (AuthService.bootstrap() itself
        # already refuses once a user exists -- this additionally makes
        # the GET page 404 too, not just reject the POST, so /auth/
        # bootstrap stops being reachable at all the moment it's used.
        app.state.bootstrap_token_hash = None
        request.session.clear(); request.session["user_id"] = user["id"]
        return RedirectResponse("/account", 303)

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request):
        user = _require_login_redirect(request)
        if not isinstance(user, dict): return user
        return render(request, "account.html", user=user, tokens=auth_service.list_api_tokens(user["id"]),
                      csrf_token=issue_csrf_token(request), new_token=None)

    @app.post("/account/api-tokens", response_class=HTMLResponse)
    @limiter.limit("20/minute")  # B0.5: token-creation abuse defense
    def account_create_api_token(request: Request, name: str = Form(...), csrf: None = Depends(require_csrf)):
        user = _require_login_redirect(request)
        if not isinstance(user, dict): return user
        _row, raw_token = auth_service.create_api_token(user["id"], name)
        # The raw token is shown exactly once, here -- never retrievable
        # again (matching ADR-001's own "never a raw access token
        # persisted" discipline applied to GitHub installation tokens).
        return render(request, "account.html", user=user, tokens=auth_service.list_api_tokens(user["id"]),
                      csrf_token=issue_csrf_token(request), new_token=raw_token)

    @app.post("/account/api-tokens/{token_id}/revoke")
    def account_revoke_api_token(request: Request, token_id: int, csrf: None = Depends(require_csrf)):
        user = _require_login_redirect(request)
        if not isinstance(user, dict): return user
        try: auth_service.revoke_api_token(user["id"], token_id)
        except AuthError as e: raise HTTPException(404, str(e))
        return RedirectResponse("/account", 303)

    @app.get("/api/whoami")
    def api_whoami(request: Request):
        """Diagnostic/proof-of-mechanism endpoint (B0.1's own concrete
        end-to-end evidence that current_user() works for both session-
        cookie and Bearer-API-token identities) -- deliberately not
        wired to gate any of the 143 pre-existing routes; that sweep is
        B0.3's own scope."""
        user = current_user(request)
        return {"auth_mode": settings.auth_mode,
                "authenticated": bool(user),
                "email": user["email"] if user else None}

    # ---- B0.2: Organizations/Tenants routes -----------------------------
    def _org_context(request: Request, org_id: int):
        """Shared membership guard every /orgs/{id}/* route below goes
        through -- not logged in redirects to login (same UX as
        /account); logged in but NOT a member of this specific org is a
        404, not a 403 (existence-hiding: a non-member never learns an
        org id is even valid, matching how a private repository behaves
        elsewhere). This is the real, data-layer cross-org isolation
        boundary this track's own instructions require -- never merely a
        hidden UI link."""
        user = _require_login_redirect(request)
        if not isinstance(user, dict): return user
        role = org_service.member_role(org_id, user["id"])
        if not role: raise HTTPException(404)
        return user, role

    @app.get("/orgs", response_class=HTMLResponse)
    def orgs_list(request: Request):
        user = _require_login_redirect(request)
        if not isinstance(user, dict): return user
        return render(request, "orgs_list.html", user=user, orgs=org_service.list_orgs_for_user(user["id"]))

    @app.get("/orgs/new", response_class=HTMLResponse)
    def orgs_new_page(request: Request):
        user = _require_login_redirect(request)
        if not isinstance(user, dict): return user
        return render(request, "orgs_new.html", error=None, csrf_token=issue_csrf_token(request))

    @app.post("/orgs", response_class=HTMLResponse)
    @limiter.limit("10/minute")  # B0.5: org-creation spam defense
    def orgs_create(request: Request, name: str = Form(...), csrf: None = Depends(require_csrf)):
        user = _require_login_redirect(request)
        if not isinstance(user, dict): return user
        try:
            org = org_service.create_org(name, user["id"])
        except OrganizationError as e:
            return render(request, "orgs_new.html", error=str(e), csrf_token=issue_csrf_token(request))
        return RedirectResponse(f"/orgs/{org['id']}", 303)

    @app.get("/orgs/{org_id}", response_class=HTMLResponse)
    def org_detail(request: Request, org_id: int):
        ctx = _org_context(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, role = ctx
        org = org_service.get_org(org_id)
        return render(request, "org_detail.html", org=org, role=role, user=user,
                      members=org_service.list_members(org_id),
                      invitations=org_service.list_pending_invitations(org_id),
                      repos=org_service.list_org_repositories(org_id),
                      unlinked_repos=org_service.list_unlinked_repositories(),
                      csrf_token=issue_csrf_token(request), roles=list(ORG_ROLES),
                      new_invitation=None, error=None)

    @app.post("/orgs/{org_id}/invite", response_class=HTMLResponse)
    @limiter.limit("20/minute")  # B0.5: invite-email spam defense
    def org_invite(request: Request, org_id: int, email: str = Form(...), role: str = Form("MEMBER"),
                    csrf: None = Depends(require_csrf)):
        ctx = _org_context(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, actor_role = ctx
        org = org_service.get_org(org_id)
        try:
            result = org_service.invite_member(org_id, user["id"], email, role)
        except OrganizationError as e:
            # INSUFFICIENT_ROLE is a real authorization failure -- the
            # same 403 every other org-management route raises on it,
            # never a 200 (even a 200 that shows an error message is the
            # wrong signal to a scripted/API caller, though the
            # underlying rejection itself was never bypassed -- the
            # service layer already refused the actual invite either
            # way). Everything else here is ordinary form-validation
            # feedback (bad email/role), rendered inline at 200.
            if e.code == "INSUFFICIENT_ROLE": raise HTTPException(403, str(e))
            return render(request, "org_detail.html", org=org, role=actor_role, user=user,
                          members=org_service.list_members(org_id),
                          invitations=org_service.list_pending_invitations(org_id),
                          repos=org_service.list_org_repositories(org_id),
                          unlinked_repos=org_service.list_unlinked_repositories(),
                          csrf_token=issue_csrf_token(request), roles=list(ORG_ROLES),
                          new_invitation=None, error=str(e))
        return RedirectResponse(f"/orgs/{org_id}", 303)

    @app.post("/orgs/{org_id}/invitations/{invitation_id}/revoke")
    def org_revoke_invitation(request: Request, org_id: int, invitation_id: int, csrf: None = Depends(require_csrf)):
        ctx = _org_context(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, _role = ctx
        try: org_service.revoke_invitation(org_id, invitation_id, user["id"])
        except OrganizationError as e: raise HTTPException(403, str(e))
        return RedirectResponse(f"/orgs/{org_id}", 303)

    @app.post("/orgs/{org_id}/members/{member_user_id}/remove")
    def org_remove_member(request: Request, org_id: int, member_user_id: int, csrf: None = Depends(require_csrf)):
        ctx = _org_context(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, _role = ctx
        try: org_service.remove_member(org_id, member_user_id, user["id"])
        except OrganizationError as e: raise HTTPException(403, str(e))
        return RedirectResponse(f"/orgs/{org_id}", 303)

    @app.post("/orgs/{org_id}/members/{member_user_id}/role")
    def org_change_member_role(request: Request, org_id: int, member_user_id: int, role: str = Form(...),
                                 csrf: None = Depends(require_csrf)):
        ctx = _org_context(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, _role = ctx
        try: org_service.change_member_role(org_id, member_user_id, role, user["id"])
        except OrganizationError as e: raise HTTPException(403, str(e))
        return RedirectResponse(f"/orgs/{org_id}", 303)

    @app.post("/orgs/{org_id}/repositories/link")
    def org_link_repository(request: Request, org_id: int, repo_id: int = Form(...), csrf: None = Depends(require_csrf)):
        ctx = _org_context(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, _role = ctx
        try: org_service.link_repository(org_id, repo_id, user["id"])
        except OrganizationError as e: raise HTTPException(403, str(e))
        return RedirectResponse(f"/orgs/{org_id}", 303)

    @app.post("/orgs/{org_id}/repositories/{repo_id}/unlink")
    def org_unlink_repository(request: Request, org_id: int, repo_id: int, csrf: None = Depends(require_csrf)):
        ctx = _org_context(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, _role = ctx
        try: org_service.unlink_repository(org_id, repo_id, user["id"])
        except OrganizationError as e: raise HTTPException(403, str(e))
        return RedirectResponse(f"/orgs/{org_id}", 303)

    @app.get("/orgs/invitations/{token}", response_class=HTMLResponse)
    def org_invitation_accept_page(request: Request, token: str):
        if settings.auth_mode != "required": raise HTTPException(404)
        row = org_service.peek_invitation(token)
        return render(request, "org_invitation_accept.html", token=token,
                      org_name=row["org_name"] if row else None, role=row["role"] if row else None, valid=bool(row))

    @app.post("/orgs/invitations/{token}")
    @limiter.limit("10/minute")  # B0.5: invitation-token brute-force defense, same family as /auth/verify's own
    def org_invitation_accept_confirm(request: Request, token: str):
        if settings.auth_mode != "required": raise HTTPException(404)
        try:
            org_user, org = org_service.accept_invitation(token)
        except OrganizationError:
            return render(request, "org_invitation_accept.html", token=token, org_name=None, role=None, valid=False)
        request.session.clear(); request.session["user_id"] = org_user["id"]
        return RedirectResponse(f"/orgs/{org['id']}", 303)

    # ---- B0.7: Secrets boundary routes ------------------------------
    # Same _org_context foundation every /orgs/* route already uses
    # (B0.2), NOT B0.3's require_role() sweep -- these are part of the
    # /orgs/* family that sweep explicitly excludes (its own established
    # non-member->404/insufficient-role->403 shape is exactly what a
    # credential-management surface needs too). OWNER/ADMIN only for
    # every one of these, including LIST -- unlike most other org
    # resources, even a secret's NAME is scoped to least-privilege by
    # default here (safe API/UI reveal semantics).
    def _secrets_ctx(request: Request, org_id: int):
        ctx = _org_context(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, role = ctx
        if role not in ("OWNER", "ADMIN"):
            raise HTTPException(403, "INSUFFICIENT_ROLE")
        return user, role

    @app.get("/orgs/{org_id}/secrets", response_class=HTMLResponse)
    def org_secrets_list(request: Request, org_id: int):
        ctx = _secrets_ctx(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, role = ctx
        return render(request, "org_secrets.html", org=org_service.get_org(org_id), role=role,
                      secrets=secrets_service.list_for_org(org_id), csrf_token=issue_csrf_token(request),
                      error=None, revealed=None)

    @app.post("/orgs/{org_id}/secrets", response_class=HTMLResponse)
    @limiter.limit("20/minute")  # B0.7: secret-creation abuse defense, same family as B0.5's other org actions
    def org_secrets_create(request: Request, org_id: int, name: str = Form(...), value: str = Form(...),
                            kind: str = Form("GENERIC"), csrf: None = Depends(require_csrf)):
        ctx = _secrets_ctx(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, role = ctx
        try:
            secrets_service.create(org_id, name, value, user["id"], kind)
        except SecretsError as exc:
            return render(request, "org_secrets.html", org=org_service.get_org(org_id), role=role,
                          secrets=secrets_service.list_for_org(org_id), csrf_token=issue_csrf_token(request),
                          error=str(exc), revealed=None)
        return RedirectResponse(f"/orgs/{org_id}/secrets", 303)

    @app.post("/orgs/{org_id}/secrets/{name}/rotate", response_class=HTMLResponse)
    @limiter.limit("20/minute")
    def org_secrets_rotate(request: Request, org_id: int, name: str, value: str = Form(...),
                            csrf: None = Depends(require_csrf)):
        ctx = _secrets_ctx(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, role = ctx
        try:
            secrets_service.rotate(org_id, name, value, user["id"])
        except SecretsError as exc:
            return render(request, "org_secrets.html", org=org_service.get_org(org_id), role=role,
                          secrets=secrets_service.list_for_org(org_id), csrf_token=issue_csrf_token(request),
                          error=str(exc), revealed=None)
        return RedirectResponse(f"/orgs/{org_id}/secrets", 303)

    @app.post("/orgs/{org_id}/secrets/{name}/revoke")
    def org_secrets_revoke(request: Request, org_id: int, name: str, csrf: None = Depends(require_csrf)):
        ctx = _secrets_ctx(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, _role = ctx
        try: secrets_service.revoke(org_id, name, user["id"])
        except SecretsError as exc: raise HTTPException(404, str(exc))
        return RedirectResponse(f"/orgs/{org_id}/secrets", 303)

    @app.post("/orgs/{org_id}/secrets/{name}/reveal", response_class=HTMLResponse)
    @limiter.limit("10/minute")  # B0.7: plaintext-reveal is the most sensitive action here -- tightest limit
    def org_secrets_reveal(request: Request, org_id: int, name: str, csrf: None = Depends(require_csrf)):
        """Shown exactly once, in the response of THIS request only --
        never persisted/cached/re-servable (matching B0.1's own API-
        token 'raw token shown once' precedent)."""
        ctx = _secrets_ctx(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, role = ctx
        try:
            plaintext = secrets_service.reveal(org_id, name, user["id"])
        except SecretsError as exc:
            return render(request, "org_secrets.html", org=org_service.get_org(org_id), role=role,
                          secrets=secrets_service.list_for_org(org_id), csrf_token=issue_csrf_token(request),
                          error=str(exc), revealed=None)
        return render(request, "org_secrets.html", org=org_service.get_org(org_id), role=role,
                      secrets=secrets_service.list_for_org(org_id), csrf_token=issue_csrf_token(request),
                      error=None, revealed={"name": name, "value": plaintext})

    # ---- B3.1: GitHub App installation (ADR-001) -------------------------
    @app.post("/orgs/{org_id}/github-installation", response_class=HTMLResponse)
    @limiter.limit("20/minute")
    def org_set_github_installation(request: Request, org_id: int, installation_id: str = Form(...),
                                      csrf: None = Depends(require_csrf)):
        """ADR-001's own "Install" flow's app-side half: an ADMIN/OWNER
        (never a MEMBER/VIEWER) records the installation_id GitHub
        assigned once this org's admin installed the App on GitHub's own
        site. The redirect TO GitHub, and GitHub's own redirect back
        with a real installation_id, both need a real registered App
        this environment doesn't have -- explicitly out of scope (see
        docs/B3_GITHUB_APP_INSTALLATION_ARCHITECTURE.md's Non-goals);
        this route only accepts the resulting id, exactly as it would
        from a real callback."""
        ctx = _org_context(request, org_id)
        if not isinstance(ctx, tuple): return ctx
        user, role = ctx
        iid = installation_id.strip()
        if not iid.isdigit():
            raise HTTPException(422, "installation_id must be a positive integer")
        try:
            org_service.set_github_installation(org_id, int(iid), user["id"])
        except OrganizationError as e:
            if e.code == "INSUFFICIENT_ROLE": raise HTTPException(403, str(e))
            raise HTTPException(400, str(e))
        return RedirectResponse(f"/orgs/{org_id}", 303)

    # ---- B4.1/B4.3: webhook PR/CI ingestion (ADR-001's "phase 2") --------
    def _resolve_repo_id_by_full_name(full_name: str) -> int | None:
        """github_owner_repo is DERIVED_TRUTH (see github_merge_service.
        py's own github_owner_repo() docstring) -- computed lazily, on
        first webhook lookup, for whichever registered repositories
        haven't been resolved yet, rather than eagerly at registration
        time (no live/local check is forced onto the register-repo path
        just to serve a feature that may never receive a single webhook
        for that repo)."""
        row = db.one("SELECT id FROM repositories WHERE github_owner_repo=?", (full_name,))
        if row:
            return row["id"]
        for r in db.all("SELECT id, repo_path FROM repositories WHERE github_owner_repo IS NULL"):
            computed = github_merge.github_owner_repo(r["repo_path"])
            if computed:
                db.execute("UPDATE repositories SET github_owner_repo=? WHERE id=?", (computed, r["id"]))
                if computed == full_name:
                    return r["id"]
        return None

    def _github_webhook_update(repo_id: int, *, pr_number: int | None = None, head_sha: str | None = None,
                                ci_status: str | None = None, mergeability: str | None = None) -> None:
        """The one place any of the three ingested event types writes --
        matches an existing merge_records row via its own EXISTING
        pr_number/head_sha columns (E10's own migration 10, kept fresh
        by the live-poll path -- B4 only ever READS them here to find
        the right row, never writes them; that stays the live-poll
        path's own job unchanged). A PR/commit this row's pr_number/
        head_sha don't (yet) know about -- e.g. a webhook arriving
        before ProjectFlow's own create_pr() call has recorded this
        exact PR -- is a safe no-op (this phase's own doc, acceptance
        criterion 2), not an error. Only ever writes the NEW webhook_*
        columns, distinct from ci_status/mergeability. Naturally
        idempotent: a plain UPDATE to "current known state," safe under
        GitHub's own at-least-once redelivery guarantee -- redelivering
        the identical payload just writes the identical values again,
        never a duplicate row (this phase's own doc, criterion 2b)."""
        row = (db.one("SELECT id FROM merge_records WHERE repository_id=? AND pr_number=?", (repo_id, pr_number))
               if pr_number is not None else None)
        if not row and head_sha:
            row = db.one("SELECT id FROM merge_records WHERE repository_id=? AND head_sha=?", (repo_id, head_sha))
        if not row:
            return
        fields, params = [], []
        if ci_status is not None: fields.append("webhook_ci_status=?"); params.append(ci_status)
        if mergeability is not None: fields.append("webhook_mergeability=?"); params.append(mergeability)
        fields.append("webhook_updated_at=CURRENT_TIMESTAMP")
        params.append(row["id"])
        db.execute(f"UPDATE merge_records SET {','.join(fields)} WHERE id=?", tuple(params))

    # B6.2 (docs/B6_TRUSTED_PROXY_SUPPORT.md): a real webhook payload for
    # the three event types this app ingests is a few KB; 1 MiB is
    # generous headroom, never unbounded.
    WEBHOOK_MAX_BODY_BYTES = 1024 * 1024

    async def _read_capped_body(request: Request, max_bytes: int) -> bytes:
        """Rejects (413) BEFORE reading the body in full, both for an
        honest oversized Content-Length declaration (cheapest case, zero
        bytes actually read) and for a request that lies about/omits
        Content-Length (bounded by capping the real stream read instead
        of one unbounded `await request.body()`)."""
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise HTTPException(413, "Payload too large")
            except ValueError:
                pass
        chunks = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(413, "Payload too large")
            chunks.append(chunk)
        return b"".join(chunks)

    @app.post("/webhooks/github")
    async def github_webhook(request: Request):
        """ADR-001's own trust boundary #4: every inbound payload is
        HMAC-verified against the App's webhook secret BEFORE any
        parsing -- untrusted network input, never trusted by default.
        No CSRF (this is a server-to-server webhook, not a browser
        session -- the HMAC signature IS this route's own CSRF-
        equivalent) and no require_role() (GitHub itself is the caller,
        not a logged-in ProjectFlow user). Dispatches on the real
        `X-Hub-Signature-256`-adjacent `X-GitHub-Event` header (not
        merely guessed from payload shape -- every App-delivered
        payload carries an `installation` object regardless of event
        type, so checking for one is not sufficient to identify an
        `installation` event specifically): `installation` events with
        action `deleted` clear the org's installation id (ADR-001's own
        "Revoke, org-initiated" flow, B3); `pull_request`/`check_run`/
        `status` events update merge_records' read-only webhook snapshot
        (B4.1-B4.3) -- ADR-001's own "worthwhile enhancement," never
        used for a merge/gate decision (see this phase's own doc, Non-
        goals). Every other event/action is accepted (200 -- the
        signature already proved it's genuinely from GitHub) and
        ignored."""
        if not settings.github_webhook_secret:
            raise HTTPException(404)
        body = await _read_capped_body(request, WEBHOOK_MAX_BODY_BYTES)
        signature = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(settings.github_webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "Invalid webhook signature")
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid JSON payload")
        event = request.headers.get("x-github-event", "")
        full_name = ((payload.get("repository") or {}).get("full_name"))
        if event == "installation":
            if payload.get("action") == "deleted" and isinstance(payload.get("installation"), dict):
                installation_id = payload["installation"].get("id")
                if installation_id is not None:
                    org_service.clear_github_installation_by_installation_id(int(installation_id))
        elif event == "pull_request" and full_name and isinstance(payload.get("pull_request"), dict):
            repo_id = _resolve_repo_id_by_full_name(full_name)
            if repo_id is not None:
                pr = payload["pull_request"]
                mergeability = "MERGED" if pr.get("merged") else (str(pr.get("mergeable_state") or "UNKNOWN")).upper()
                _github_webhook_update(repo_id, pr_number=pr.get("number"), mergeability=mergeability)
        elif event == "check_run" and full_name and isinstance(payload.get("check_run"), dict):
            repo_id = _resolve_repo_id_by_full_name(full_name)
            if repo_id is not None:
                cr = payload["check_run"]
                # Same mapping _parse()'s own live-poll path already
                # uses (github_merge_service.py's FAIL_CONCLUSIONS) --
                # one shared vocabulary for "what does this conclusion
                # mean", not a second one invented here.
                conclusion = (cr.get("conclusion") or "").upper()
                ci_status = "PASS" if conclusion == "SUCCESS" else "FAIL" if conclusion in FAIL_CONCLUSIONS else "PENDING"
                prs = cr.get("pull_requests") or []
                if prs:
                    for pr in prs:
                        _github_webhook_update(repo_id, pr_number=pr.get("number"), ci_status=ci_status)
                else:
                    _github_webhook_update(repo_id, head_sha=cr.get("head_sha"), ci_status=ci_status)
        elif event == "status" and full_name:
            repo_id = _resolve_repo_id_by_full_name(full_name)
            if repo_id is not None:
                state = str(payload.get("state") or "").upper()
                ci_status = "PASS" if state == "SUCCESS" else "FAIL" if state in ("FAILURE", "ERROR") else "PENDING"
                _github_webhook_update(repo_id, head_sha=payload.get("sha"), ci_status=ci_status)
        return {"ok": True}

    # ---- B3.2: health/readiness -------------------------------------------
    @app.get("/health")
    def health():
        """No auth, no CSRF, no tenant data -- a real orchestrator/load-
        balancer liveness probe, cheap enough to poll frequently (one
        trivial query, never a full page render). Fails closed: a
        broken DB connection is a real 503, never a blind 200 -- the
        whole point of a health check is to be honest when something
        IS wrong."""
        try:
            db.one("SELECT 1")
        except Exception as exc:
            return JSONResponse({"status": "unhealthy", "error": str(exc)}, status_code=503)
        return {"status": "ok", "auth_mode": settings.auth_mode}

    def repo(repo_id):
        row = db.one("SELECT * FROM repositories WHERE id=? AND enabled=1", (repo_id,))
        if not row: raise HTTPException(404, "Enabled repository not found")
        git.validate_repo(row["repo_path"]); return row
    def agent_row(wid):
        row=db.one("SELECT w.*,r.repo_name,r.repo_path FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.id=?",(wid,))
        if not row: raise HTTPException(404); return row
        return row
    def integration_row(iid):
        row=db.one("SELECT i.*,r.repo_name,r.repo_path FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id WHERE i.id=?",(iid,))
        if not row: raise HTTPException(404)
        return row
    def invalidate(iid): db.execute("UPDATE integration_workspaces SET ready_for_main=0,verified_commit=NULL,verified_at=NULL,status='TESTING',updated_at=CURRENT_TIMESTAMP WHERE id=?",(iid,))
    TEST_RUN_LABELS={"QUEUED":"Starting tests...","RUNNING":"Running tests...","PASS":"Tests PASS","FAIL":"Tests FAILED","TIMEOUT":"Tests timed out","SKIPPED":"Tests skipped"}
    OPERATION_RUNNING_LABELS={"MERGE_LATEST":"Merging latest changes...","PUSH_INTEGRATION":"Pushing...","MARK_READY_FOR_MAIN":"Validating readiness...","CREATE_PR":"Creating PR...","MERGE_PR":"Merging..."}
    def integration_current_action(iid):
        """Button-state-ux section 6: whichever action on this Integration
        is most worth surfacing right now -- a currently RUNNING one if
        any (test run and/or a tracked Operation), else the most recent
        terminal result of any kind. Never a client-side guess: every
        field here comes straight from test_runs/operations."""
        candidates=[]
        tr=db.one("SELECT * FROM test_runs WHERE workspace_type='integration' AND workspace_id=? ORDER BY id DESC LIMIT 1",(iid,))
        if tr:
            running=tr["status"] in ("QUEUED","RUNNING")
            candidates.append({"kind":"Run Tests","started_at":tr["started_at"] or tr["finished_at"] or "","is_running":running,
                                "label":TEST_RUN_LABELS.get(tr["status"],tr["status"]),"is_failed":tr["status"] in ("FAIL","TIMEOUT"),
                                "is_succeeded":tr["status"]=="PASS","url":f"/test-runs/{tr['id']}/log"})
        for optype,kind in (("MERGE_LATEST","Merge Latest Changes"),("PUSH_INTEGRATION","Push Integration Branch"),("MARK_READY_FOR_MAIN","Mark Ready for Main")):
            op=ops.latest("integration",iid,optype)
            if not op: continue
            running=op["status"] in ("QUEUED","RUNNING")
            label=OPERATION_RUNNING_LABELS[optype] if running else (op["result_summary"] if op["status"]=="SUCCEEDED" else (op["error"] or "Failed"))
            candidates.append({"kind":kind,"started_at":op["started_at"] or "","is_running":running,"label":label,
                                "is_failed":op["status"]=="FAILED","is_succeeded":op["status"]=="SUCCEEDED","url":None})
        if not candidates: return None
        running=[c for c in candidates if c["is_running"]]
        return max(running or candidates,key=lambda c:c["started_at"])
    def safe_details(worktree_path):
        path=Path(worktree_path)
        empty={"head":None,"status":[],"modified":[],"untracked":[],"commits":[]}
        if not path.exists(): return empty
        try: return git.details(path)
        except GitSafetyError: return empty
    def task_row(tid):
        row=db.one("SELECT * FROM tasks WHERE id=?",(tid,))
        if not row: raise HTTPException(404,"Task not found")
        return row
    def sandbox_row(sid):
        row=db.one("SELECT s.*,r.repo_name FROM sandboxes s LEFT JOIN repositories r ON r.id=s.repository_id WHERE s.id=?",(sid,))
        if not row: raise HTTPException(404,"Sandbox not found")
        return row
    def task_workspaces(tid): return db.all("SELECT w.*,r.repo_name,r.repo_path FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.task_id=? ORDER BY w.created_at",(tid,))
    def task_sandboxes(tid): return db.all("SELECT * FROM sandboxes WHERE task_id=? ORDER BY id",(tid,))
    def task_integration_row(task_id):
        return db.one("SELECT * FROM task_integrations WHERE task_id=? ORDER BY id DESC LIMIT 1",(task_id,))
    def format_countdown(iso):
        """Single place that turns a cleanup_eligible_at timestamp into a
        human countdown -- reused by /sandboxes, /sandboxes/{id} and
        /tasks/{id} so the three views can never drift on the wording."""
        if not iso: return None
        try:
            secs=int((datetime.fromisoformat(iso)-datetime.now(timezone.utc)).total_seconds())
        except Exception: return None
        return "due" if secs<=0 else f"{secs//3600}h {secs%3600//60}m"
    def sandbox_view(sb):
        """The one place that turns a `sandboxes` row into everything a
        template needs to render it (outputs/ports/countdown/open URLs) --
        called by /sandboxes, /sandboxes/{id} and /tasks/{id} so all three
        views read the exact same Sandbox/SandboxPort/output state."""
        outputs=sandboxes.outputs(sb["id"]); ports_=ports.ports_for(sb["id"])
        primary=db.one("SELECT s.*,r.repo_name FROM sandbox_sources s JOIN repositories r ON r.id=s.repository_id WHERE s.sandbox_id=? ORDER BY s.id LIMIT 1",(sb["id"],))
        # A sandbox contract can name its single "open this" output
        # anything (qa_center_url, backend_url, frontend_url, ...) --
        # never require it be literally named frontend_url to be openable
        # (QA Center sandbox incident, section 9/11: "the button must use
        # the sandbox's trusted output, never require inspecting Advanced
        # for the port"). frontend_url wins when declared; otherwise the
        # first declared *_url output that isn't backend/hardware-only
        # stands in.
        frontend_url=outputs.get("frontend_url") or next(
            (v for k,v in outputs.items() if k.endswith("_url") and k not in ("backend_url","hardware_api_url","lan_url")), None)
        return {"row":sb,"outputs":outputs,"ports":ports_,"primary_source":primary,
                "cleanup_countdown":format_countdown(sb["cleanup_eligible_at"]),
                "backend_url":outputs.get("backend_url"),"frontend_url":frontend_url,
                "hardware_api_url":outputs.get("hardware_api_url") or outputs.get("lan_url")}
    def sandbox_current_commits(sandbox_id):
        cur={}
        for s in db.all("SELECT * FROM sandbox_sources WHERE sandbox_id=?",(sandbox_id,)):
            try: cur[s["repository_id"]]=git.head(s["worktree_path"])
            except Exception: pass
        return cur
    def integration_readiness(iid):
        """The single readiness computation for one repo's Integration
        Workspace -- backs BOTH the POST ready-for-main gate and the
        Task Detail readiness checklist, so the template never derives a
        second, independently-calculated status. Test-gate PASS/FAIL
        (including baseline-waiver awareness, section 16-17) is delegated
        to TaskDecisionService.integration_gate_status() -- the one
        authoritative place that decision lives, never recomputed here."""
        i=integration_row(iid)
        clean=not git.status(i["worktree_path"]).strip(); no_conflicts=not git.conflict_files(i["worktree_path"])
        sources_current=True
        for s in db.all("SELECT s.*,w.branch FROM integration_sources s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.integration_id=?",(iid,)):
            current=git.head(i["repo_path"],s["branch"])
            if current!=s["merged_commit"] or not git.is_ancestor(i["worktree_path"],current): sources_current=False
        ti=db.one("SELECT task_id FROM task_integrations WHERE id=?",(i["task_integration_id"],)) if i["task_integration_id"] else None
        gate=decision.integration_gate_status(i,ti["task_id"] if ti else None)
        head=gate["head"] or git.head(i["worktree_path"])
        return {"integration":i,"head":head,"clean":clean,"no_conflicts":no_conflicts,"sources_current":sources_current,
                "tests_pass":gate["tests_pass"],"tests_status":gate["tests_status"],"failures":gate["failures"],
                "tests_passed":gate.get("tests_passed",0),"tests_required":gate["tests_required"],"summary":gate.get("summary") or {},
                "ready":clean and no_conflicts and sources_current and gate["tests_pass"]}
    def sandbox_for_workspace(wid):
        return db.one("SELECT * FROM sandboxes WHERE owner_type='AGENT_WORKSPACE' AND owner_id=? ORDER BY id DESC LIMIT 1",(wid,))
    def latest_test_status(workspace_type,workspace_id):
        row=db.one("SELECT status FROM test_runs WHERE workspace_type=? AND workspace_id=? ORDER BY id DESC LIMIT 1",(workspace_type,workspace_id))
        return row["status"] if row else "NOT_RUN"
    def manual_verification_status(sandbox_id,worktree_path):
        """Latest manual verification for a sandbox. Staleness is never
        stored -- recomputed here by comparing its recorded source_commit
        to the source branch's CURRENT git HEAD, same rule as sandbox
        staleness. A PASS never survives new commits on the branch."""
        row=db.one("SELECT * FROM manual_verifications WHERE sandbox_id=? ORDER BY id DESC LIMIT 1",(sandbox_id,))
        if not row: return {"status":"NOT_RUN","row":None}
        try: current=git.head(worktree_path)
        except Exception: current=None
        stale=current is not None and current!=row["source_commit"]
        return {"status":"STALE" if stale else row["result"],"row":row}
    def workspace_readiness(w):
        """The one place that reads a workspace's four independent
        verification signals -- sandbox, automated tests, manual
        verification, sandbox-contract presence -- so workspace_detail,
        task_detail and next_action_code never compute a conflicting
        answer for the same workspace."""
        sb=sandbox_for_workspace(w["id"])
        try: configured=load_sandbox_contract(Path(w["repo_path"])) is not None
        except SandboxContractError: configured=True  # misconfigured contract is not "absent"
        manual={"status":"NOT_RUN","row":None}; sb_view=None
        if sb:
            sb_view=sandbox_view(sb)
            manual=manual_verification_status(sb["id"],w["worktree_path"])
        # sandbox_configured = the repo declares a sandbox: contract at all
        # (drives the "not configured for this repository" empty state).
        # sandbox_required = this workspace actually needs a RUNNING
        # sandbox to be considered testable -- false when the user
        # explicitly chose profile NONE, even on a repo that has a
        # contract, so a deliberate no-runtime workspace is never treated
        # as perpetually blocked on "Create Sandbox".
        required=configured and w.get("sandbox_profile")!="NONE"
        return {"agent_ready":w["status"]=="READY","sandbox":sb,"sandbox_view":sb_view,
                "sandbox_configured":configured,"sandbox_required":required,"sandbox_status":sb["status"] if sb else "NOT_CREATED",
                "automated_status":latest_test_status("agent",w["id"]),"manual":manual}
    def task_verification(tid): return db.one("SELECT * FROM verification_reports WHERE task_id=? AND workspace_id IS NULL ORDER BY id DESC LIMIT 1",(tid,))
    def workspace_verification(wid): return db.one("SELECT * FROM verification_reports WHERE workspace_id=? ORDER BY id DESC LIMIT 1",(wid,))
    def effective_verification(tid,workspaces):
        """Task-level note wins; for the common single-workspace task, the
        one workspace's own report stands in for the task's -- never
        invented, only ever what an agent/user actually recorded."""
        tv=task_verification(tid) if tid else None
        if tv: return tv
        if len(workspaces)==1: return workspace_verification(workspaces[0]["id"])
        return None

    NEXT_ACTION_COPY={
        "MARK_READY":("Agent chưa Mark Ready — code chưa được coi là hoàn thành.",None,None),
        "NO_SANDBOX_CONTRACT_WAIT":("Repo này không có sandbox: contract. Verify thủ công qua worktree, sau đó theo dõi Integration.",None,None),
        "NO_SANDBOX_CONTRACT_INTEGRATE":("Repo này không có sandbox: contract. Verify thủ công qua worktree, sau đó tích hợp.","Create Integration","integration"),
        "CREATE_SANDBOX":("Code đã được agent hoàn thành nhưng chưa có runtime verification.","Create Sandbox","create-sandbox"),
        "SANDBOX_FAILED":("Sandbox tạo thất bại.","Xem Logs","sandbox"),
        "SANDBOX_STARTING":("Sandbox đang khởi động, chờ vài giây rồi tải lại.",None,None),
        "SANDBOX_NOT_RUNNING":("Sandbox không chạy.","Start Sandbox","sandbox"),
        "RUN_TESTS":("Sandbox đã sẵn sàng — nên chạy automated tests trước khi kiểm tra thủ công.","Run Tests","run-tests"),
        "TESTS_FAILING":("Automated tests đang FAIL.","Xem log test","tests"),
        "OPEN_APP_VERIFY":("Automated tests PASS — mở app và kiểm tra thủ công theo hướng dẫn.","Open App and Verify","open-app"),
        "MANUAL_FAILING":("Kiểm tra thủ công FAIL — quay lại agent workspace để sửa.",None,None),
        "VERIFIED_STANDALONE":("Agent code đã được verify (sandbox + test).","Create Integration","legacy-integration"),
        "CREATE_INTEGRATION":("Đã verify — sẵn sàng tích hợp.","Create Integration","integration"),
        "VIEW_INTEGRATION":("Integration đang chạy — còn điều kiện chưa đạt, xem Integration Readiness.","Xem Integration","integration"),
        "PREPARE_PR":("Mọi điều kiện đã đạt.","Prepare PR (push + tạo Pull Request)","github-flow"),
    }
    def next_action_code(r,integration_exists=None,ready_for_main=False):
        """Pure state -> action mapping. Deterministic, no AI: same inputs
        always produce the same recommendation."""
        if not r["agent_ready"]: return "MARK_READY"
        if not r.get("sandbox_required",r["sandbox_configured"]):
            if integration_exists is None: return "NO_SANDBOX_CONTRACT_WAIT"
            return "VIEW_INTEGRATION" if integration_exists else "NO_SANDBOX_CONTRACT_INTEGRATE"
        st=r["sandbox_status"]
        if st=="NOT_CREATED": return "CREATE_SANDBOX"
        if st=="FAILED": return "SANDBOX_FAILED"
        if st in ("CREATED","PROVISIONING","STARTING"): return "SANDBOX_STARTING"
        # CLEANUP_ELIGIBLE is a retention countdown, not a runtime state --
        # the real container is still up and usable for the whole grace
        # window (state-consistency audit finding: never conflate the two).
        if st not in ("RUNNING","CLEANUP_ELIGIBLE"): return "SANDBOX_NOT_RUNNING"
        if r["automated_status"]=="NOT_RUN": return "RUN_TESTS"
        if r["automated_status"]=="FAIL": return "TESTS_FAILING"
        if r["manual"]["status"] in ("NOT_RUN","STALE"): return "OPEN_APP_VERIFY"
        if r["manual"]["status"]=="FAIL": return "MANUAL_FAILING"
        if integration_exists is None: return "VERIFIED_STANDALONE"
        if not integration_exists: return "CREATE_INTEGRATION"
        if not ready_for_main: return "VIEW_INTEGRATION"
        return "PREPARE_PR"
    def resolve_next_action(code,*,wid=None,tid=None,sandbox_id=None):
        """Turns a symbolic action tag into a real href/method for the
        context it is shown in (workspace page vs task page)."""
        text,label,tag=NEXT_ACTION_COPY[code]
        href=None; method="GET"
        if tag=="create-sandbox":
            href=f"/api/tasks/{tid}/workspaces/{wid}/create-sandbox" if tid else f"/api/workspaces/{wid}/create-sandbox"; method="POST"
        elif tag=="sandbox" and sandbox_id: href=f"/sandboxes/{sandbox_id}"
        elif tag=="run-tests" and wid: href=f"/api/workspaces/{wid}/test"; method="POST"
        elif tag=="tests" and wid: href=f"/workspaces/{wid}"
        elif tag=="open-app" and sandbox_id: href=f"/sandboxes/{sandbox_id}"
        elif tag=="integration" and tid: href=f"/tasks/{tid}"
        elif tag=="legacy-integration": href="/integrations"
        elif tag=="github-flow": href="/help#github-flow"
        return {"code":code,"text":text,"label":label,"href":href,"method":method}

    # ---------------------------------------------------- Control plane
    # RISK_PROFILES is just the tuple of valid names for form validation --
    # the actual LOW/NORMAL/HIGH -> required-gates policy lives only in
    # TaskDecisionService.RISK_GATES now (decision.requires_qa/
    # requires_integration below); a second, hand-typed copy of that table
    # here had already drifted from it once (workflow-decision-UX audit)
    # before this fix -- never re-add one.
    RISK_PROFILES=("LOW","NORMAL","HIGH")
    def latest_session_for_workspace(wid):
        return db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1",(wid,))
    def activity_summary(sid, max_len=200):
        """One clean, human-readable line of 'what is this session doing
        right now' -- reads the LIVE in-process buffer (never the
        possibly-stale/empty DB transcript_tail column, which is only
        refreshed on WS disconnect) and strips ANSI/control sequences
        before ever rendering it outside the real xterm terminal
        (section 21). None when there is genuinely no transcript yet."""
        text=agent_sessions.live_tail(sid)
        if not text: return None
        lines=[l.strip() for l in strip_ansi(text).splitlines() if l.strip()]
        return lines[-1][:max_len] if lines else None
    def has_legacy_brief(t):
        return bool(t.get("brief_goal") or t.get("brief_context") or t.get("brief_requirements") or
                    t.get("brief_acceptance_criteria") or t.get("brief_out_of_scope") or t.get("brief_test_plan") or t.get("brief_risks"))
    def sandbox_summary_line(w):
        """One line for the Builder prompt's SANDBOX section: real
        profile/status/URL if a sandbox already exists for this
        workspace, else just the declared profile, else nothing worth
        saying (None -- the section is omitted, never fabricated)."""
        if not w: return None
        sb=db.one("SELECT * FROM sandboxes WHERE owner_type='AGENT_WORKSPACE' AND owner_id=? ORDER BY id DESC LIMIT 1",(w["id"],))
        if not sb: return w.get("sandbox_profile") or None
        line=f"{sb['profile']} · {sb['status']}"
        try:
            outputs=json.loads(sb["source_manifest_json"] or "{}").get("outputs",{})
            url=outputs.get("backend_url") or outputs.get("frontend_url")
            if url: line+=f" · {url}"
        except Exception: pass
        return line
    def _spec_context_section(t):
        """Spec Layer (S6): only the relevant slice for THIS Task's
        linked feature -- never the whole specs/ tree. Empty for the
        common case of a Task with no spec linkage at all (never an
        empty/confusing section). The trailing SPEC RULES block is
        fixed, Workspace-Manager-owned text the user's own Task intent
        (confined to its own TASK section above) can never reach into or
        override, same discipline as RULES/COMPLETION already use."""
        feature_id=(t.get("spec_feature_id") or "").strip()
        if not feature_id: return []
        try:
            registry=SpecRegistry(specs_root).load()
        except SpecError as exc:
            return ["## SPEC",f"(spec registry failed to load: {exc} -- proceed with caution, report SPEC_DRIFT if behavior is unclear)",""]
        feature=registry.feature(feature_id)
        if not feature:
            return ["## SPEC",f"(unknown feature id {feature_id} -- report SPEC_DRIFT, do not invent behavior)",""]
        req_ids=spec_id_list(t.get("spec_requirement_ids")); acc_ids=spec_id_list(t.get("spec_acceptance_ids")); inv_ids=spec_id_list(t.get("spec_invariant_ids"))
        lines=["## SPEC",f"Feature: {feature_id} -- {feature.get('title','')} (v{feature.get('version')}, {feature.get('status')})"]
        if feature.get("summary"): lines.append(str(feature["summary"]).strip())
        scope=feature.get("scope") or {}
        if scope.get("includes"): lines+=["Allowed scope (includes):",*[f"- {x}" for x in scope["includes"]]]
        if scope.get("excludes"): lines+=["Out of scope (excludes):",*[f"- {x}" for x in scope["excludes"]]]
        reqs=[(rid,registry.requirement(rid)) for rid in req_ids]
        if reqs: lines+=["Requirements:",*[f"- {rid}: {r['text'].strip()}" for rid,r in reqs if r]]
        accs=[(aid,registry.acceptance_criterion(aid)) for aid in acc_ids]
        if accs: lines+=["Acceptance Criteria:",*[f"- {aid}: {a['text'].strip()}" for aid,a in accs if a]]
        invs=[(iid,registry.invariant(iid)) for iid in inv_ids]
        if invs: lines+=["Invariants (must hold at all times):",*[f"- {iid}: {i['text'].strip()}" for iid,i in invs if i]]
        lines+=["","SPEC RULES (mandatory):",
                "- Do not invent unspecified behavior.",
                "- Do not weaken acceptance criteria or invariants.",
                "- Do not silently expand scope beyond what is listed above.",
                "- Do not modify unrelated behavior.",
                "- If the implementation and this approved spec disagree, report SPEC_DRIFT instead of guessing.",""]
        return lines
    def render_agent_prompt(t,repo_row=None,workspace=None,sandbox_line=None):
        """Deterministic template fill -- never an actual model call, and
        the user's own task intent text is NEVER rewritten, only wrapped
        with real, recorded context (trusted Workspace Manager execution
        context -> the effective task intent verbatim -> this repo's own
        AGENTS.md rules, if known -> completion requirements). The user
        still reviews/edits the result before any agent is launched
        (never auto-start an agent). 'Do not allow user-controlled Task
        text to override Workspace Manager safety rules': the intent is
        confined to its own TASK section, and RULES/COMPLETION are always
        separate, later sections the user's own text cannot reach into.

        Task Title fallback: effective intent is the Implementation
        Prompt when non-empty, else the Task title itself -- this path
        (and the finer BRANCH/WORKTREE/SANDBOX/ROLE context below) is used
        for every Task that has no legacy structured brief content, title-
        only ones included, never leaving a Builder with only a bare
        title and no real prompt. Tasks created before this UX existed
        (implementation_prompt empty AND legacy brief_* fields set) keep
        the exact old GOAL/CONTEXT/.../RISKS rendering, unchanged --
        'structured old tasks still load'."""
        if (t.get("implementation_prompt") or "").strip() or not has_legacy_brief(t):
            intent=effective_task_prompt(t)
            parts=[f"# Task: {t['title']}","","## TASK",intent,"","## TASK TITLE",t["title"],""]
            if repo_row: parts+=["## REPOSITORY",f"{repo_row['repo_name']} ({repo_row['repo_path']})",""]
            if workspace: parts+=["## BRANCH",workspace["branch"],"","## WORKTREE",workspace["worktree_path"],""]
            if workspace:
                # E8.5.12: WORKSPACE ISOLATION -- prompt-level statement of
                # a boundary already enforced structurally (this worktree
                # IS the Builder's cwd; the canonical checkout is a
                # different directory the launcher never gives it access
                # to) -- defense in depth, never depended on alone.
                parts+=["## WORKSPACE ISOLATION",
                        "- You are operating in a ProjectFlow-managed task worktree.",
                        "- Modify only this worktree.",
                        "- Do not access or edit the canonical checkout.",
                        "- Do not create additional git worktrees.",
                        "- Do not switch branches.",
                        "- Do not merge.",
                        "- Do not rebase.",
                        "- Do not push unless ProjectFlow explicitly instructs it.",
                        "- Complete only the assigned Task scope.",""]
            if sandbox_line: parts+=["## SANDBOX",sandbox_line,""]
            if workspace:
                parts+=["## ROLE",workspace.get("role") or workspace.get("agent") or "",""]
                instr=(workspace.get("builder_instructions") or "").strip()
                if instr: parts+=["## BUILDER INSTRUCTIONS (this workspace only)",instr,""]
            parts+=_spec_context_section(t)
            # E8.8/E8.9: bounded architecture/design/test-contract slice
            # for THIS Task's own governing PlanItem/requirement ids --
            # never the whole project's engineering state. A Task with
            # no change_id (every legacy/manual Task) or no materialized
            # PlanItem gets nothing extra here (render_lines returns []).
            parts+=task_execution_context_builder.render_lines(t["id"],t.get("change_id"))
            rules=None
            if repo_row:
                try:
                    p=Path(repo_row["repo_path"])/"AGENTS.md"
                    if p.is_file(): rules=p.read_text(errors="replace")[:6000].strip()
                except Exception: rules=None
            parts.append("## RULES")
            parts.append("Read AGENTS.md and PROJECT.yaml before modifying code.")
            if rules: parts+=["",rules]
            parts+=["","## COMPLETION",
                    "- implement task","- run appropriate tests","- review diff","- commit changes",
                    "- report WHAT_CHANGED","- report TESTS_RUN","- report HOW_TO_VERIFY","- report EXPECTED_RESULT","- report RISKS",
                    "- Submit for Review (use the format in templates/agent-completion-report.md)."]
            return "\n".join(parts)
        parts=[f"# Task: {t['title']}",""]
        if t["brief_goal"]: parts+=["## GOAL",t["brief_goal"],""]
        if t["brief_context"]: parts+=["## CONTEXT",t["brief_context"],""]
        if t["brief_requirements"]: parts+=["## REQUIREMENTS",t["brief_requirements"],""]
        if t["brief_acceptance_criteria"]: parts+=["## ACCEPTANCE_CRITERIA",t["brief_acceptance_criteria"],""]
        if t["brief_out_of_scope"]: parts+=["## OUT_OF_SCOPE",t["brief_out_of_scope"],""]
        if t["brief_test_plan"]: parts+=["## TEST_PLAN",t["brief_test_plan"],""]
        if t["brief_risks"]: parts+=["## RISKS",t["brief_risks"],""]
        parts.append("When your source change is complete, report back using the format in templates/agent-completion-report.md.")
        return "\n".join(parts)
    def regenerate_agent_prompt(tid,repo_row=None):
        """Recompute the derived, composed TASK-LEVEL agent_prompt (no
        specific Builder Workspace yet) and persist it as a new `prompts`
        row stamped with the exact brief_version it was generated from --
        same discipline generate_prompt already established, just
        callable from anywhere a Task's prompt-relevant state just
        changed (create, prompt edit). Once a Builder Workspace exists,
        its own live per-workspace prompt (role/instructions/sandbox
        included) is what actually matters -- see workspace_agent_prompt()."""
        t=task_row(tid); prompt=render_agent_prompt(t,repo_row)
        db.execute("INSERT INTO prompts(task_id,prompt_type,brief_version,content) VALUES(?,?,?,?)",(tid,"BUILDER",t["brief_version"],prompt))
        db.execute("UPDATE tasks SET agent_prompt=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(prompt,tid))
        db.event("task",tid,"PROMPT_GENERATED",f"brief_version={t['brief_version']}")
        return prompt
    def workspace_agent_prompt(w,t=None,repo_row=None,note_exited_without_report=False):
        """Live per-Builder-Workspace effective prompt -- always freshly
        computed (never stored/cached), so a role/instructions/sandbox
        edit is reflected immediately with no separate staleness to
        track. Only Review/QA evidence is version-pinned; this display
        text is not.

        Return to Builder (spec section 11): when the reviewer most
        recently returned FIX_REQUIRED for this exact workspace, its
        findings are appended as their own section so the next Builder
        session's prompt is the original task intent + what the
        reviewer asked to change -- never a bare 'resume' with no
        context. A PASS/STALE/no-review workspace gets no such section.

        `note_exited_without_report` (section 6) is decided by the
        caller, not re-derived here -- by the time a resumed session's
        prompt is being computed, a brand-new AgentSession row already
        exists and is "the latest session", so re-querying it here would
        always see the wrong (new) one."""
        t=t or task_row(w["task_id"])
        if repo_row is None:
            try: repo_row=repo(w["repository_id"])
            except HTTPException: repo_row=None
        prompt=render_agent_prompt(t,repo_row,workspace=w,sandbox_line=sandbox_summary_line(w))
        review=decision.latest_review(w["id"])
        if review and review["status"]=="FIX_REQUIRED":
            findings=(review["findings"] or "").strip() or "(no written findings)"
            prompt+="\n\n## REVIEW FINDINGS (fix required -- address these, then Submit for Review again)\n"+findings+"\n"
        # EXITED-without-report resume (section 6): the previous process
        # ended without ever getting a completion report into
        # verification_reports -- tell the freshly-resumed agent exactly
        # that, so it inspects what's already there instead of quietly
        # redoing finished work or re-reading the whole task as if this
        # were a first attempt.
        if note_exited_without_report and not (review and review["status"]=="FIX_REQUIRED"):
            prompt+=("\n\n## PREVIOUS SESSION ENDED WITHOUT A COMPLETION REPORT\n"
                      "Your previous session exited without a persisted completion report.\n"
                      "Please inspect the existing workspace state and submit the required completion report for the current HEAD.\n"
                      "Do not redo completed work unnecessarily.\n")
        return prompt
    def render_review_prompt(t,w,report):
        """Deterministic template (section 6): the Task's own prompt/brief,
        source branch/commit, and the Builder's own completion report --
        never a diff computed by an LLM, only real recorded facts."""
        head=git.head(w["worktree_path"])
        parts=[f"# Review: {t['title']}",f"Branch: {w['branch']} @ {head[:12]}",""]
        if (t.get("implementation_prompt") or "").strip() or not has_legacy_brief(t): parts+=["## TASK PROMPT",effective_task_prompt(t),""]
        elif t["brief_acceptance_criteria"]: parts+=["## ACCEPTANCE_CRITERIA",t["brief_acceptance_criteria"],""]
        if report:
            parts+=["## Builder report","WHAT_CHANGED: "+ (report["what_changed"] or "—"),"FILES_CHANGED: "+(report["files_changed"] or "—"),
                     "TESTS_RUN: "+(report["tests_run"] or report["automated_tests"] or "—"),"RISKS: "+(report["risks"] or "—"),""]
        else:
            parts+=["## Builder report","(no completion report submitted yet)",""]
        parts.append("Evaluate correctness, completeness, scope, regressions, missing tests, and unsafe/unexpected changes. Report REVIEW_PASS, FIX_REQUIRED, or BLOCKED. Do not modify source unless explicitly switched to repair mode.")
        return "\n".join(parts)

    @app.exception_handler(GitSafetyError)
    @app.exception_handler(GitCommandError)
    async def git_error(request, exc): return HTMLResponse(f"<h1>Action blocked</h1><pre>{str(exc)}</pre><a href='javascript:history.back()'>Back</a>", status_code=409)

    @app.exception_handler(SandboxError)
    @app.exception_handler(SandboxContractError)
    async def sandbox_error(request, exc):
        code = getattr(exc, "code", "SANDBOX_ERROR")
        if request.url.path.startswith("/api/"): return JSONResponse({"ok":False,"code":code,"message":str(exc)},status_code=409)
        return HTMLResponse(f"<h1>Sandbox action blocked</h1><pre>{code}: {str(exc)}</pre><a href='javascript:history.back()'>Back</a>", status_code=409)

    @app.exception_handler(ChangeError)
    @app.exception_handler(WorkProductError)
    @app.exception_handler(WorkflowError)
    @app.exception_handler(PlannerError)
    @app.exception_handler(SpecLifecycleError)
    @app.exception_handler(ArchitectureDesignError)
    @app.exception_handler(TestDesignError)
    @app.exception_handler(AutonomousExecutionError)
    @app.exception_handler(WorktreeManagerError)
    @app.exception_handler(ReviewError)
    @app.exception_handler(ReviewFixError)
    @app.exception_handler(IntegrationError)
    @app.exception_handler(ReleaseError)
    @app.exception_handler(ProductAcceptanceError)
    @app.exception_handler(IncidentError)
    @app.exception_handler(ExecutionWaveError)
    async def engineering_domain_error(request, exc):
        """Phase E1/E3/E4/E5/E6/E7/E8/E8.5/E9/E10/E11/E12/E13's Change/
        WorkProduct/Workflow/Plan/Spec-Proposal/Architecture/Design/
        Test-Design/Autonomous-Execution/Worktree/Review-Fix/
        Integration/Release/Product-Acceptance/Incident/ExecutionWave
        API is a pure JSON surface (E1.7: 'API/service
        correctness first, no large UI yet') -- a clean 400 + message,
        never the HTML 'Action blocked' page the older form-posting
        routes use."""
        return JSONResponse({"ok":False,"message":str(exc)},status_code=400)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        repo_ids=_visible_repo_ids(request)
        agents=_filter_rows(db.all("SELECT w.*,r.repo_name FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.status NOT IN ('CLOSED','DONE') ORDER BY w.updated_at DESC"),repo_ids,"repository_id")
        ints=_filter_rows(db.all("SELECT i.*,r.repo_name FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id WHERE i.status!='CLOSED' ORDER BY i.updated_at DESC"),repo_ids,"repository_id")
        # B5.2 (docs/B5_TENANT_ISOLATION_COMPLETENESS.md): filtered
        # BEFORE aggregation, closing the B1.1(b) residual -- never a
        # global COUNT computed then hidden after. task_ids covers the
        # indirect-ownership case (a REPOSITORY_TEST-owned sandbox has
        # no direct repository_id, only a task_id).
        task_ids=_visible_task_ids(request)
        running_sandboxes=sandboxes.running_count(repo_ids,task_ids)
        cleanup_pending=sandboxes.count("status='CLEANUP_ELIGIBLE'",repo_ids,task_ids)
        # Dashboard is Task-centric (section 48): every count below comes
        # from TaskDecisionService.evaluate() (via task_card_view), the
        # same source Kanban/List/Detail use -- never a raw worktree
        # count standing in for Task state, never a second dashboard-only
        # tally that could drift from the real gate engine.
        task_rows=_filter_rows(db.all("SELECT * FROM tasks WHERE status!='CANCELLED'"),_visible_task_ids(request))
        cards=[task_card_view(t) for t in task_rows]
        by_status={s:sum(1 for c in cards if c["status"]==s) for s in ("BACKLOG","ACTIVE","BLOCKED","READY_FOR_MAIN","DONE")}
        summary={"active":len(agents),"ready":sum(x["status"]=="READY" for x in agents),"testing":sum(x["status"]=="TESTING" for x in ints),"main":sum(bool(x["ready_for_main"]) for x in ints),
                 "tasks":len(task_rows),"sandboxes":running_sandboxes,"cleanup":cleanup_pending,
                 "backlog":by_status["BACKLOG"],"tasks_active":by_status["ACTIVE"],"blocked":by_status["BLOCKED"],
                 "tasks_ready_for_main":by_status["READY_FOR_MAIN"],"done_recent":by_status["DONE"],
                 "builders_running":sum(1 for c in cards for w in c["workspaces"] if not w["ready"]),
                 "review_pending":sum(1 for c in cards if c["decision"]["next_action"]["action"] in ("SUBMIT_FOR_REVIEW","START_REVIEW")),
                 "qa_pending":sum(1 for c in cards if c["decision"]["next_action"]["action"]=="START_QA"),
                 "integrations_running":sum(1 for c in cards if c["stage"] in ("INTEGRATION","MERGING")),
                 "tasks_fix_required":sum(1 for c in cards if c["needs_fix"])}
        return render(request,"dashboard.html",agents=agents,integrations=ints,summary=summary,cards=cards,first_run=not db.one("SELECT id FROM repositories LIMIT 1"))

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request): return render(request, "help.html")

    def _repositories_with_identity_flags(rows: list) -> list:
        """B7.1: two LIVE, computed-not-stored signals per row -- never
        persisted, so they're always current, and a directory that
        comes back (or a row that gets cleanly rebound) stops showing a
        flag on the very next read, with nothing to invalidate. `path_missing`:
        a real `Path.is_dir()` check. `possible_duplicate_of`: another
        row (by id) sharing the same real `git_fingerprint` whose OWN
        path is ALSO currently real (an ambiguous case -- see
        register()'s own docstring; a fingerprint match against a
        MISSING row is instead auto-rebound there, never surfaced as a
        loose end here)."""
        by_fp: dict[str, list[int]] = {}
        for r in rows:
            if r["git_fingerprint"]:
                by_fp.setdefault(r["git_fingerprint"], []).append(r["id"])
        out = []
        for r in rows:
            r = dict(r)
            r["path_missing"] = not Path(r["repo_path"]).is_dir()
            dupes = [rid for rid in by_fp.get(r["git_fingerprint"] or "", []) if rid != r["id"]] if r["git_fingerprint"] else []
            r["possible_duplicate_of"] = dupes[0] if dupes and not r["path_missing"] else None
            out.append(r)
        return out

    @app.get("/repositories", response_class=HTMLResponse)
    def repositories(request: Request):
        rows=_filter_rows(db.all("SELECT * FROM repositories ORDER BY repo_name"),_visible_repo_ids(request))
        return render(request,"repositories.html",repositories=_repositories_with_identity_flags(rows),discovered=discover_repositories(settings.root))
    @app.post("/api/repositories")
    def register(request: Request, repo_path: str=Form(...), repo_name: str=Form(""), default_branch: str=Form("main"), _csrf: None = Depends(_mutating_csrf)):
        """B7.1 (docs/B7_WORKSPACE_REPOSITORY_IDENTITY.md): a renamed/
        moved repo directory used to re-register as an orphaned
        duplicate, disconnected from every Task/Change/Release row the
        OLD row's id still owned. Now resolved by real evidence, never
        guessed: (a) the SAME path re-registers exactly as before
        (update-in-place, unchanged). (b) a NEW path whose fingerprint
        matches EXACTLY ONE existing row, and that row's OWN old path
        is confirmed missing from disk right now, is deterministic
        evidence of a move -- rebinds that EXISTING row (same id, same
        org link, same history) instead of inserting a new one;
        github_owner_repo is cleared so it recomputes fresh (closing
        B4.1's own remote-change staleness gap). (c) a fingerprint
        match where the OTHER row's path still exists is ambiguous
        (could be a real accidental duplicate, or a deliberately-kept-
        separate second clone) -- registers as its own new row, exactly
        like before, NEVER silently merged; surfaced as a live
        possible-duplicate flag on /repositories instead. (d) no
        fingerprint match -- ordinary new registration, unchanged."""
        # B0.3: registering a brand-new repository carries no org
        # reference at all yet (it doesn't belong to one until a later
        # /orgs/{id}/repositories/link) -- identity only, no role to check.
        _require_login_only(request)
        path=git.validate_repo(repo_path); default_branch=git.validate_branch(default_branch)
        if not git.base_exists(path,default_branch): raise GitSafetyError("Default branch missing")
        fingerprint=git.repo_fingerprint(path)
        existing_same_path=db.one("SELECT id FROM repositories WHERE repo_path=?",(str(path),))
        if not existing_same_path and fingerprint:
            candidates=db.all("SELECT * FROM repositories WHERE git_fingerprint=?",(fingerprint,))
            missing=[c for c in candidates if not Path(c["repo_path"]).is_dir()]
            if len(missing)==1:
                old=missing[0]
                db.execute(
                    "UPDATE repositories SET repo_path=?,enabled=1,repo_name=?,default_branch=?,git_fingerprint=?,github_owner_repo=NULL WHERE id=?",
                    (str(path),slugify(repo_name or path.name),default_branch,fingerprint,old["id"]))
                db.event("repository",old["id"],"REPOSITORY_REBOUND",f"old_path={old['repo_path']} new_path={path}")
                return RedirectResponse("/repositories",303)
        db.execute(
            "INSERT INTO repositories(repo_name,repo_path,default_branch,git_fingerprint) VALUES(?,?,?,?) "
            "ON CONFLICT(repo_path) DO UPDATE SET enabled=1,repo_name=excluded.repo_name,default_branch=excluded.default_branch,git_fingerprint=excluded.git_fingerprint",
            (slugify(repo_name or path.name),str(path),default_branch,fingerprint))
        return RedirectResponse("/repositories",303)
    @app.get("/api/repositories")
    def api_repos(request: Request): return _filter_rows(db.all("SELECT * FROM repositories"),_visible_repo_ids(request))

    # ------------------------------------------- Repository Runtime & Sandbox
    def check_dependency_cycle(self_repo_name, dependency_names):
        """Cross-repo cycle check (section 23) -- needs every OTHER
        registered repo's own contract, so it lives here (main.py already
        has the full repository registry) rather than inside
        RepositoryContractEditor itself. Rejects only a real cycle back to
        self_repo_name; unrelated dependency graphs elsewhere are none of
        this repo's concern."""
        seen = set()
        stack = list(dependency_names)
        while stack:
            name = stack.pop()
            if name == self_repo_name: return True
            if name in seen: continue
            seen.add(name)
            row = db.one("SELECT repo_path FROM repositories WHERE repo_name=? AND enabled=1", (name,))
            if not row: continue
            try: settings_ = contract_editor.read_sandbox_settings(Path(row["repo_path"]))
            except ContractEditError: continue
            stack.extend(d["repo"] for d in settings_.get("runtime_dependencies", []))
        return False
    @app.get("/repositories/{rid}/runtime", response_class=HTMLResponse)
    def repository_runtime(request: Request, rid: int, _authz: None = Depends(require_read_role("repository", "rid"))):
        r = repo(rid)
        try:
            current = contract_editor.read_sandbox_settings(Path(r["repo_path"]))
            load_error = None
        except ContractEditError as exc:
            current = None; load_error = "; ".join(exc.errors)
        registered = db.all("SELECT id,repo_name FROM repositories WHERE enabled=1 AND id!=?", (rid,))
        history = db.all("SELECT * FROM workspace_events WHERE entity_type='repository' AND entity_id=? AND action='REPOSITORY_CONTRACT_UPDATED' ORDER BY id DESC LIMIT 10", (rid,))
        diff = request.query_params.get("diff") == "1"
        raw_text = None
        if diff or request.query_params.get("view") == "raw":
            try: raw_text = contract_editor.load(Path(r["repo_path"]))["raw_text"]
            except ContractEditError: pass
        git_dirty = None
        try: git_dirty = bool(git.status(r["repo_path"]).strip())
        except Exception: pass
        test_sandbox = db.one("SELECT * FROM sandboxes WHERE owner_type='REPOSITORY_TEST' AND owner_id=? ORDER BY id DESC LIMIT 1", (rid,))
        return render(request, "repository_runtime.html", r=r, current=current, load_error=load_error,
                      registered=registered, history=history, raw_text=raw_text, git_dirty=git_dirty,
                      test_sandbox=test_sandbox)

    def _parse_runtime_form(form) -> dict:
        enabled = form.get("enabled") == "on"
        dep_repos = form.getlist("dep_repo")
        dep_profiles = form.getlist("dep_profile")
        dep_modes = form.getlist("dep_mode")
        deps = [
            {"repo": repo_.strip(), "profile": (prof or "BACKEND").strip(), "mode": (mode or "KNOWN_GOOD_MAIN").strip()}
            for repo_, prof, mode in zip(dep_repos, dep_profiles, dep_modes) if repo_.strip()
        ]
        services = [s.strip() for s in (form.get("services") or "").split(",") if s.strip()]
        profile_name = (form.get("profile_name") or "").strip().upper()
        own_service = services[0] if services else profile_name.lower()
        # Ports/health/outputs (section 3: "health endpoint if schema
        # supports it... runtime output labels" ARE normal-UI settings,
        # not Advanced-only) -- the user only ever types the container
        # port + health path; the HOST port range is never user-supplied
        # (section 24/26) -- port_specs() already falls back to a wide,
        # safe default range (20000-29999) whenever `range` is omitted,
        # so this deliberately never writes one.
        ports = {}
        own_port = (form.get("own_port") or "").strip()
        if own_port.isdigit():
            ports[own_service] = {"container": int(own_port)}
        health = {}
        health_path = (form.get("health_path") or "").strip()
        if health_path:
            health[own_service] = {"path": health_path}
        outputs = {}
        if services and profile_name:
            outputs[f"{profile_name.lower()}_url"] = {"service": own_service}
        return {
            "enabled": enabled,
            "profile_name": profile_name,
            "services": services,
            "runtime_dependencies": deps,
            "seed_default": (form.get("seed_default") or "").strip() or None,
            "auto_provision": form.get("auto_provision") == "on",
            "ports": ports, "health": health, "outputs": outputs,
        }
    @app.post("/api/repositories/{rid}/runtime-sandbox")
    async def save_runtime_sandbox(request: Request, rid: int, _authz: None = Depends(require_role("repository", "rid", "MEMBER"))):
        r = repo(rid)
        form = await request.form()
        proposed = _parse_runtime_form(form)
        # section 7: production target never accidentally selected --
        # a profile literally named PRODUCTION (or a dependency mode
        # pointed at one) is refused outright, never silently accepted.
        if proposed["enabled"] and proposed["profile_name"] == "PRODUCTION":
            raise GitSafetyError("Refusing to save a sandbox profile named PRODUCTION -- sandboxes are never production targets")
        registered_names = {x["repo_name"] for x in db.all("SELECT repo_name FROM repositories WHERE enabled=1")}
        dep_names = [d["repo"] for d in proposed["runtime_dependencies"]]
        if check_dependency_cycle(r["repo_name"], dep_names):
            raise GitSafetyError(f"Runtime dependency graph would form a cycle back to {r['repo_name']}")
        try:
            diff = contract_editor.write_sandbox_settings(Path(r["repo_path"]), proposed, self_repo_name=r["repo_name"], registered_repo_names=registered_names)
        except ContractEditError as exc:
            raise GitSafetyError("; ".join(exc.errors))
        db.event("repository", rid, "REPOSITORY_CONTRACT_UPDATED",
                  f"repo={r['repo_name']} fields=sandbox before_sha={diff['before_sha256'][:12]} after_sha={diff['after_sha256'][:12]} operator=ui")
        return RedirectResponse(f"/repositories/{rid}/runtime?diff=1", 303)
    @app.post("/api/repositories/{rid}/runtime-sandbox/test")
    def test_runtime_sandbox(rid: int, _authz: None = Depends(require_role("repository", "rid", "MEMBER"))):
        r = repo(rid)
        try:
            current = contract_editor.read_sandbox_settings(Path(r["repo_path"]))
        except ContractEditError as exc:
            raise GitSafetyError("; ".join(exc.errors))
        if not current["enabled"]:
            raise GitSafetyError("Sandbox is not enabled for this repository -- save a configuration first")
        existing = db.one("SELECT * FROM sandboxes WHERE owner_type='REPOSITORY_TEST' AND owner_id=? AND status IN ('CREATED','PROVISIONING','STARTING','RUNNING') ORDER BY id DESC LIMIT 1", (rid,))
        if existing:
            return RedirectResponse(f"/sandboxes/{existing['id']}", 303)
        commit = git.head(r["repo_path"])
        source = SourceSpec(repository_id=rid, role="test-configuration", branch=r["default_branch"], commit_sha=commit,
                             worktree_path=r["repo_path"], repo_path=r["repo_path"], source_type="REPOSITORY_TEST")
        try:
            sid = sandboxes.create(task_id=None, owner_type="REPOSITORY_TEST", owner_id=rid, profile=current["profile_name"], provider=source)
        except SandboxError as exc:
            raise GitSafetyError(str(exc))
        if sid is None:
            raise GitSafetyError("Sandbox profile resolved to NONE -- nothing to test")
        db.event("repository", rid, "REPOSITORY_CONTRACT_TESTED", f"repo={r['repo_name']} profile={current['profile_name']} sandbox={sid}")
        sandboxes.provision(sid)
        return RedirectResponse(f"/sandboxes/{sid}", 303)

    @app.get("/workspaces", response_class=HTMLResponse)
    def workspaces(request: Request):
        repo_ids=_visible_repo_ids(request)
        return render(request,"workspaces.html",
                       workspaces=_filter_rows(db.all("SELECT w.*,r.repo_name FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id ORDER BY w.updated_at DESC"),repo_ids,"repository_id"),
                       repositories=_filter_rows(db.all("SELECT * FROM repositories WHERE enabled=1"),repo_ids),
                       tasks=_filter_rows(db.all("SELECT id,title FROM tasks WHERE status NOT IN ('MERGED','CANCELLED') ORDER BY title"),_visible_task_ids(request)))
    @app.post("/api/workspaces")
    def create_workspace(request:Request,repository_id:int=Form(...),agent:str=Form(...),task_name:str=Form(...),base_branch:str=Form("main"), _csrf: None = Depends(_mutating_csrf)):
        _require_org_role_for_repo(request, repository_id, "MEMBER")
        r=repo(repository_id); agent=slugify(agent); task=slugify(task_name)
        if agent not in settings.agents: raise GitSafetyError("Agent is not allowed")
        branch,path,commit=git.create_agent(r["repo_path"],agent,task,base_branch)
        try: wid=db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status) VALUES(?,?,?,?,?,?,?,?,?)",(repository_id,agent,task,branch,str(path),base_branch,commit,commit,"CREATED"))
        except Exception:
            if not git.status(path).strip(): git.close(r["repo_path"],path)
            raise
        db.event("agent",wid,"WORKSPACE_CREATED",branch); return RedirectResponse(f"/workspaces/{wid}",303)
    @app.get("/api/workspaces")
    def api_workspaces(request: Request): return _filter_rows(db.all("SELECT * FROM agent_workspaces"),_visible_repo_ids(request),"repository_id")
    @app.get("/workspaces/{wid}",response_class=HTMLResponse)
    def workspace_detail(request:Request,wid:int, _authz: None = Depends(require_read_role("workspace", "wid"))):
        """Section 39: Task Status, Workspace Status, Sandbox Status and
        Test Status are shown as four clearly separate values -- Task
        Status/review gate state come only from decision.evaluate() (the
        `task_decision`/`builder` values below); `readiness` stays scoped
        to this workspace's own runtime signals (sandbox/automated tests/
        manual verification), a different concern from the Review/QA
        gate. Neither is allowed to stand in for the other."""
        w=agent_row(wid); details=safe_details(w["worktree_path"])
        runs=db.all("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=? ORDER BY id DESC",(wid,))
        readiness=workspace_readiness(w)
        report=workspace_verification(wid) or (task_verification(w["task_id"]) if w["task_id"] else None)
        manual_history=db.all("SELECT * FROM manual_verifications WHERE workspace_id=? ORDER BY id DESC",(wid,))
        task_decision=decision.evaluate(w["task_id"]) if w["task_id"] else None
        builder=next((b for b in task_decision["builders"] if b["id"]==wid),None) if task_decision else None
        integration_exists=None if not w["task_id"] else bool(task_decision["task_integration"])
        ready_for_main=bool(task_decision and task_decision["ready_for_main"])
        code=next_action_code(readiness,integration_exists,ready_for_main)
        action=resolve_next_action(code,wid=wid,tid=w["task_id"],sandbox_id=readiness["sandbox"]["id"] if readiness["sandbox"] else None)
        sessions=db.all("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 10",(wid,))
        review_history=decision.review_history(wid)
        live_prompt=workspace_agent_prompt(w,task_row(w["task_id"]),repo(w["repository_id"])) if w["task_id"] else None
        # One authoritative session-state computation for this page,
        # whether or not the workspace belongs to a Task (builder.session/
        # agent_status only exist in the task-scoped case) -- Workspace
        # Detail UX audit section 1/4: the whole "Current Action" block
        # reads only this, never re-derives session state itself.
        session=sessions[0] if sessions else None
        live_session=session if session and session["status"] in LIVE_SESSION_STATUSES else None
        agent_status=session["status"] if session else "NOT_STARTED"
        detected_report=parse_completion_report(agent_sessions.live_tail(session["id"])) if session and w["status"]!="READY" else None
        recovery_state="COMPLETION_REQUIRED" if agent_status in ("EXITED","FAILED") and w["status"]!="READY" and not detected_report else None
        manual_ready_check=validate_manual_ready(w) if recovery_state else None
        # Section 18: Task Detail and Workspace Detail must never disagree
        # about the one authoritative primary action -- once this
        # workspace belongs to a Task, `user_state` (the exact same
        # user_task_state(decision.evaluate()) the Task page renders) is
        # what the Current Action panel shows, not a second,
        # independently-computed opinion. Only a workspace with no Task at
        # all (nothing for TaskDecisionService to evaluate) still falls
        # back to the legacy per-workspace `next_action` ladder below.
        task_user_state=user_task_state(task_decision) if task_decision else None
        return render(request,"workspace_detail.html",w=w,details=details,runs=runs,readiness=readiness,report=report,
                      manual_history=manual_history,next_action=action,sessions=sessions,
                      task_decision=task_decision,task_user_state=task_user_state,builder=builder,review_history=review_history,live_prompt=live_prompt,
                      session=session,live_session=live_session,agent_status=agent_status,detected_report=detected_report,
                      recovery_state=recovery_state,manual_ready_check=manual_ready_check)
    @app.get("/api/workspaces/{wid}")
    def api_workspace(wid:int, _authz: None = Depends(require_read_role("workspace", "wid"))): return agent_row(wid)
    @app.post("/api/workspaces/{wid}/ready")
    def ready(wid:int, _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        w=agent_row(wid); head=git.head(w["worktree_path"]); db.execute("UPDATE agent_workspaces SET status='READY',ready_for_integration=1,last_commit=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,wid)); db.event("agent",wid,"READY_MARKED",head)
        recompute_task_status(w.get("task_id"))
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/test")
    def test_agent(wid:int, _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))): w=agent_row(wid); runner.start("agent",wid,Path(w["worktree_path"])); return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/create-sandbox")
    def create_workspace_sandbox_standalone(wid:int,profile:str=Form(""), _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        w=agent_row(wid)
        task_default=None
        if w["task_id"]:
            t=db.one("SELECT default_sandbox_profile FROM tasks WHERE id=?",(w["task_id"],)); task_default=t["default_sandbox_profile"] if t else None
        sid=auto_create_sandbox(w["task_id"],w["repository_id"],w["repo_path"],"AGENT_WORKSPACE",wid,w["role"] or w["agent"],w["branch"],w["last_commit"],w["worktree_path"],profile.strip().upper() or None,task_default)
        if sid: db.execute("UPDATE agent_workspaces SET sandbox_profile=(SELECT profile FROM sandboxes WHERE id=?) WHERE id=?",(sid,wid))
        return RedirectResponse(f"/workspaces/{wid}",303)
    def _insert_verification_report(w,t,status_upper,head,*,what_changed="",files_changed="",tests_run="",automated_tests="",how_to_verify="",expected_result="",test_data="",runtime_requirements="NONE",risks="",ready_source="AGENT_SUBMITTED",operator=None):
        """Shared by the normal Submit-for-Review path and the manual
        EXITED-without-report recovery fallback -- one insert, one
        status-transition, one audit shape, regardless of which UI action
        produced the report. `ready_source` records which path it was
        (never changes what READY itself requires: commit_sha is always
        the exact pinned HEAD, still validated clean by the caller).

        Spec Layer (S8): snapshots the Task's spec_* linkage onto this
        evidence row at the moment it's produced -- Evidence stays
        traceable (Spec -> Task -> Agent execution -> Verification ->
        Evidence) even if the Task's own linkage is edited afterward.
        A Task with no linkage (spec_feature_id NULL) stamps NULL/empty
        here too -- this is never required for the insert to succeed."""
        wid=w["id"]
        db.execute("INSERT INTO verification_reports(task_id,workspace_id,work_status,what_changed,files_changed,tests_run,automated_tests,how_to_verify,expected_result,test_data,runtime_requirements,risks,commit_sha,brief_version,ready_source,operator,spec_feature_id,spec_version,spec_requirement_ids,spec_acceptance_ids,spec_invariant_ids) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (w["task_id"],wid,status_upper,what_changed.strip(),files_changed.strip(),tests_run.strip(),automated_tests.strip(),how_to_verify.strip(),expected_result.strip(),test_data.strip(),runtime_requirements.strip().upper() or "NONE",risks.strip(),head,t["brief_version"] if t else None,ready_source,operator,
                    (t or {}).get("spec_feature_id"),(t or {}).get("spec_version"),(t or {}).get("spec_requirement_ids") or "[]",(t or {}).get("spec_acceptance_ids") or "[]",(t or {}).get("spec_invariant_ids") or "[]"))
        db.event("agent",wid,"VERIFICATION_REPORT_ADDED",f"{status_upper} source={ready_source}")
        if status_upper=="READY" and w["status"]!="READY":
            db.execute("UPDATE agent_workspaces SET status='READY',ready_for_integration=1,last_commit=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,wid))
            db.event("agent",wid,"SUBMITTED_FOR_REVIEW",f"{head} source={ready_source}"); recompute_task_status(w.get("task_id"))
            # E8.12: capture a durable CODE_CHANGE WorkProduct at the exact
            # moment a Builder Workspace becomes READY -- same hook for a
            # manual Submit-for-Review and an eventual autonomous one
            # alike (autonomous_execution_service is constructed later in
            # this same create_app() scope but only ever CALLED at request
            # time, well after construction finishes).
            autonomous_execution_service.record_code_change_work_product(w,t,head,files_changed)
    @app.post("/api/workspaces/{wid}/verification-report")
    def submit_workspace_report(wid:int,work_status:str=Form("READY"),what_changed:str=Form(""),files_changed:str=Form(""),tests_run:str=Form(""),automated_tests:str=Form(""),how_to_verify:str=Form(""),expected_result:str=Form(""),test_data:str=Form(""),runtime_requirements:str=Form("NONE"),risks:str=Form(""), _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        """Submit for Review (section 14/15): the Builder completion
        report (WHAT_CHANGED/FILES_CHANGED/TESTS_RUN/HOW_TO_VERIFY/
        EXPECTED_RESULT/RISKS) is what workspace becomes READY_FOR_REVIEW
        from. Pinned to the exact commit_sha and brief_version at
        submission time (section 15) -- both are what
        TaskDecisionService.builder_view() later compares against to
        decide a downstream Review/QA/Integration result is still valid.
        WORK_STATUS: READY requires a clean git worktree (section 14) --
        an uncommitted change can never silently become "ready"."""
        w=agent_row(wid); t=task_row(w["task_id"]) if w["task_id"] else None
        status_upper=work_status.strip().upper() or "READY"
        if status_upper=="READY" and git.status(w["worktree_path"]).strip():
            raise GitSafetyError("Git worktree must be clean before submitting for review (uncommitted changes present)")
        head=git.head(w["worktree_path"])
        _insert_verification_report(w,t,status_upper,head,what_changed=what_changed,files_changed=files_changed,tests_run=tests_run,automated_tests=automated_tests,how_to_verify=how_to_verify,expected_result=expected_result,test_data=test_data,runtime_requirements=runtime_requirements,risks=risks,ready_source="AGENT_SUBMITTED")
        return RedirectResponse(f"/workspaces/{wid}",303)

    MANUAL_READY_BLOCKERS={
        "WORKTREE_MISSING":"Worktree không còn tồn tại trên máy này.",
        "HEAD_UNRESOLVED":"Không đọc được HEAD hiện tại của worktree.",
        "BRANCH_MISMATCH":"Worktree đang ở branch khác với branch của Builder này.",
        "MERGE_CONFLICT":"Worktree đang có merge conflict chưa resolve.",
        "UNCOMMITTED_CHANGES":"Worktree có thay đổi chưa commit.",
        "NO_SOURCE_CHANGES":"Chưa có commit nào mới so với base -- chưa có gì để Submit for Review.",
    }
    def validate_manual_ready(w):
        """Section 2 (EXITED-without-report manual fallback): everything
        that must be true about the real git worktree before a human can
        stand in for a missing agent completion report. Never trusts the
        UI state that got the user here -- re-derives every check from
        the actual worktree on disk, in the same order the blockers are
        documented, first failure wins. Returns {"ok","blocker","detail","head"}."""
        path=Path(w["worktree_path"])
        if not path.is_dir():
            return {"ok":False,"blocker":"WORKTREE_MISSING","detail":MANUAL_READY_BLOCKERS["WORKTREE_MISSING"],"head":None}
        try:
            branch_result=git.git(path,"rev-parse","--abbrev-ref","HEAD",check=False)
            current_branch=branch_result.stdout.strip()
        except GitCommandError:
            current_branch=""
        if not current_branch or branch_result.returncode:
            return {"ok":False,"blocker":"HEAD_UNRESOLVED","detail":MANUAL_READY_BLOCKERS["HEAD_UNRESOLVED"],"head":None}
        if current_branch!=w["branch"]:
            return {"ok":False,"blocker":"BRANCH_MISMATCH","detail":f"{MANUAL_READY_BLOCKERS['BRANCH_MISMATCH']} (worktree={current_branch}, builder={w['branch']})","head":None}
        if git.conflict_files(path):
            return {"ok":False,"blocker":"MERGE_CONFLICT","detail":MANUAL_READY_BLOCKERS["MERGE_CONFLICT"],"head":None}
        if git.status(path).strip():
            return {"ok":False,"blocker":"UNCOMMITTED_CHANGES","detail":MANUAL_READY_BLOCKERS["UNCOMMITTED_CHANGES"],"head":None}
        try:
            head=git.head(path)
        except GitCommandError:
            return {"ok":False,"blocker":"HEAD_UNRESOLVED","detail":MANUAL_READY_BLOCKERS["HEAD_UNRESOLVED"],"head":None}
        if head==w["base_commit"]:
            return {"ok":False,"blocker":"NO_SOURCE_CHANGES","detail":MANUAL_READY_BLOCKERS["NO_SOURCE_CHANGES"],"head":head}
        return {"ok":True,"blocker":None,"detail":"","head":head}

    @app.post("/api/workspaces/{wid}/mark-ready-manual")
    def mark_ready_manual(wid:int,what_changed:str=Form(...),how_to_verify:str=Form(...),tests_run:str=Form("Not run"),expected_result:str=Form(""),risks:str=Form("None known"), _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        """[Mark Ready for Review] manual fallback (section 1/2/12): shown
        only when AgentSession EXITED (or FAILED) without a persisted or
        detected completion report and the Builder isn't already READY --
        a recovery path for real, already-complete source work, never a
        force-pass button. Re-validates the real worktree from scratch
        server-side regardless of what the page showed (section 12) --
        the same checks that decide whether the button is even shown are
        run again here, because client state is never trusted for
        something this consequential. What changed / How to verify are
        the only two required fields (section 3); Tests run defaults to
        'Not run', Risks to 'None known' so a genuinely simple, already-
        complete change never needs a user to invent boilerplate."""
        w=agent_row(wid); t=task_row(w["task_id"]) if w["task_id"] else None
        session=decision.latest_session(wid)
        db.event("agent",wid,"BUILDER_MANUAL_READY_REQUESTED",f"task={w.get('task_id')} session={session['id'] if session else None} operator=ui")
        if w["status"]=="READY":
            raise GitSafetyError("Builder is already Submitted for Review")
        check=validate_manual_ready(w)
        if not check["ok"]:
            db.event("agent",wid,"BUILDER_MANUAL_READY_BLOCKED",f"task={w.get('task_id')} session={session['id'] if session else None} blocker={check['blocker']} operator=ui")
            raise GitSafetyError(f"Cannot mark ready: {check['detail']}")
        if not what_changed.strip() or not how_to_verify.strip():
            db.event("agent",wid,"BUILDER_MANUAL_READY_BLOCKED",f"task={w.get('task_id')} session={session['id'] if session else None} blocker=MISSING_SUMMARY operator=ui")
            raise GitSafetyError("What changed and How to verify are required")
        head=check["head"]
        _insert_verification_report(w,t,"READY",head,what_changed=what_changed,tests_run=tests_run or "Not run",how_to_verify=how_to_verify,expected_result=expected_result,risks=risks or "None known",ready_source="MANUAL_CONFIRMATION",operator="ui")
        db.event("agent",wid,"BUILDER_MANUAL_READY_SUCCEEDED",f"task={w.get('task_id')} session={session['id'] if session else None} commit={head} operator=ui")
        return RedirectResponse(f"/tasks/{w['task_id']}" if w.get("task_id") else f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/close")
    def close_agent(wid:int, _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))): w=agent_row(wid); git.close(w["repo_path"],w["worktree_path"]); db.execute("UPDATE agent_workspaces SET status='CLOSED',closed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(wid,)); db.event("agent",wid,"WORKSPACE_CLOSED"); return RedirectResponse("/workspaces",303)
    @app.post("/api/workspaces/{wid}/create-task")
    def create_task_from_workspace(wid:int, _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        """Legacy-workspace migration (section 21): wraps an UNASSIGNED
        workspace (task_id IS NULL) in a brand-new Task, title prefilled
        from its own task_name. No branch/worktree/sandbox is touched or
        recreated -- this only sets agent_workspaces.task_id."""
        w=agent_row(wid)
        if w["task_id"]: raise GitSafetyError("Workspace already belongs to a Task")
        title=w["task_name"].replace("-"," ").replace("_"," ").strip() or w["task_name"]
        slug=slugify(title)
        # Section 5: a Task with >=1 Builder Workspace is never BACKLOG --
        # this one already has code underway, so it starts ACTIVE.
        tid=db.execute("INSERT INTO tasks(slug,title,description,status) VALUES(?,?,?,?)",(slug,title,f"Created from existing Agent Workspace #{wid} ({w['agent']}/{w['repo_name']}).","ACTIVE"))
        db.execute("UPDATE agent_workspaces SET task_id=? WHERE id=?",(tid,wid))
        db.event("task",tid,"TASK_CREATED_FROM_WORKSPACE",str(wid)); db.event("agent",wid,"ATTACHED_TO_TASK",str(tid))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/workspaces/{wid}/attach-task")
    def attach_workspace_to_task(wid:int,task_id:int=Form(...), _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        w=agent_row(wid)
        if w["task_id"]: raise GitSafetyError("Workspace already belongs to a Task")
        t=task_row(task_id)
        db.execute("UPDATE agent_workspaces SET task_id=? WHERE id=?",(task_id,wid))
        db.event("agent",wid,"ATTACHED_TO_TASK",str(task_id)); recompute_task_status(task_id)
        return RedirectResponse(f"/tasks/{task_id}",303)

    # ------------------------------------------------------ Review / QA
    @app.post("/api/workspaces/{wid}/start-review")
    def start_review(wid:int,reviewer_agent:str=Form(...), _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        """[Start Review] (section 6/11): only after Builder submitted
        (status=READY). Inserts a NEW review_runs row -- a fresh review
        cycle after a fix is a new row, never an overwrite of the old
        one's evidence (section 18). Default mode is READ_ONLY: no new
        worktree is created here at all; a reviewer only ever gets a
        worktree if explicitly escalated to repair mode via the normal
        Create Agent Workspace flow."""
        w=agent_row(wid)
        if w["status"]!="READY": raise GitSafetyError("Builder has not submitted for review yet")
        t=task_row(w["task_id"]) if w["task_id"] else None
        report=workspace_verification(wid)
        prompt=render_review_prompt(t,w,report) if t else render_review_prompt({"title":w["task_name"],"brief_acceptance_criteria":""},w,report)
        head=git.head(w["worktree_path"])
        bv=t["brief_version"] if t else None
        rid=db.execute("INSERT INTO review_runs(task_id,workspace_id,reviewer_type,reviewer_agent,brief_version,reviewed_commit,status,findings) VALUES(?,?,?,?,?,?,?,?)",
                        (w["task_id"],wid,"BUILDER_WORKSPACE",slugify(reviewer_agent),bv,head,"RUNNING",prompt))
        if t: db.execute("INSERT INTO prompts(task_id,workspace_id,prompt_type,brief_version,content) VALUES(?,?,?,?,?)",(t["id"],wid,"REVIEWER",bv,prompt))
        # Role & Capability Catalog (E2 section 13): advisory only,
        # never blocking -- reviewer_agent is, and stays, a free-text
        # label (often a human name), and Start Review never actually
        # launches a process for it (READ_ONLY, no worktree). Only
        # worth checking -- and only worth an audit event -- when the
        # label names a real, launchable provider this catalog actually
        # knows about; an arbitrary human name is a HUMAN actor by
        # definition and has nothing to validate against.
        if is_known_provider(reviewer_agent):
            check=roles_catalog.validate_assignment(reviewer_agent,"REVIEWER")
            if not check["valid"]:
                db.event("agent",wid,"ROLE_ASSIGNMENT_REJECTED",f"provider={reviewer_agent} role=REVIEWER missing={check['missing_required_capabilities']} (advisory, not blocked)")
        db.event("agent",wid,"REVIEW_STARTED",f"run={rid} reviewer={reviewer_agent}")
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/submit-review")
    def submit_review(wid:int,result:str=Form(...),notes:str=Form(""), _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        """Reviewer result (section 6/7/17): REVIEW_PASS/FIX_REQUIRED/
        BLOCKED, completing the most recent RUNNING review_runs row for
        this workspace (never overwriting an earlier, already-completed
        one -- that stays queryable history). FIX_REQUIRED returns the
        Task to the Builder; a later commit or Brief bump independently
        makes this same row STALE at read time (TaskDecisionService),
        never something this route has to remember to invalidate."""
        w=agent_row(wid)
        if result not in ("PASS","FIX_REQUIRED","BLOCKED"): raise GitSafetyError("Invalid review result")
        run=db.one("SELECT * FROM review_runs WHERE workspace_id=? ORDER BY id DESC LIMIT 1",(wid,))
        if not run or run["status"] not in ("PENDING","RUNNING"): raise GitSafetyError("No review in progress for this workspace")
        db.execute("UPDATE review_runs SET status=?,findings=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",(result,notes.strip(),run["id"]))
        db.event("agent",wid,"REVIEW_SUBMITTED",f"run={run['id']} result={result}")
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/tasks/{tid}/start-qa")
    def start_qa(tid:int,tester_agent:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """[Start Tester] (section 8/12/19): QA is Task-level (it may
        cover cross-repo behavior), gated on every required Builder's
        review being PASS-and-current. Primarily reads the sandbox +
        exact source manifest already in place -- no new worktree unless
        the tester must change test code/fixtures, which stays a manual,
        explicit Create Agent Workspace outside this route."""
        t=task_row(tid); d=decision.evaluate(tid)
        if d["stage"] not in ("QA","INTEGRATION","MERGING","COMPLETE") or any(b["review_status"]!="PASS" for b in d["builders"]):
            raise GitSafetyError("QA requires every required Builder's review to be PASS and current")
        sbxs=task_sandboxes(tid); sandbox_id=next((s["id"] for s in sbxs if s["status"] in ("RUNNING","CLEANUP_ELIGIBLE")),None)
        manifest=db.one("SELECT source_manifest_json FROM sandboxes WHERE id=?",(sandbox_id,))["source_manifest_json"] if sandbox_id else "{}"
        qid=db.execute("INSERT INTO qa_runs(task_id,brief_version,source_manifest,sandbox_id,tester_agent,status) VALUES(?,?,?,?,?,?)",
                        (tid,t["brief_version"],manifest,sandbox_id,slugify(tester_agent) if tester_agent else "qa","RUNNING"))
        # Role & Capability Catalog (E2 section 15): QA in ProjectFlow is
        # a human/manual verification by default (tester_agent defaults
        # to "qa", a placeholder label, and no code path launches a
        # process for it) -- advisory-only check, same reasoning as
        # start_review, only when tester_agent actually names a known
        # launchable provider.
        if is_known_provider(tester_agent):
            check=roles_catalog.validate_assignment(tester_agent,"QA_VERIFIER")
            if not check["valid"]:
                db.event("task",tid,"ROLE_ASSIGNMENT_REJECTED",f"provider={tester_agent} role=QA_VERIFIER missing={check['missing_required_capabilities']} (advisory, not blocked)")
        db.event("task",tid,"QA_STARTED",f"run={qid} tester={tester_agent}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/submit-qa")
    def submit_qa(tid:int,result:str=Form(...),notes:str=Form(""),manual_result:str=Form(""),hardware_result:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        if result not in ("PASS","FAIL","BLOCKED"): raise GitSafetyError("Invalid QA result")
        run=db.one("SELECT * FROM qa_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",(tid,))
        if not run or run["status"] not in ("PENDING","RUNNING"): raise GitSafetyError("No QA run in progress for this Task")
        db.execute("UPDATE qa_runs SET status=?,notes=?,manual_result=?,hardware_result=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                   (result,notes.strip(),manual_result.strip() or None,hardware_result.strip() or None,run["id"]))
        db.event("task",tid,"QA_SUBMITTED",f"run={run['id']} result={result}")
        return RedirectResponse(f"/tasks/{tid}",303)

    # ------------------------------------------------- Agent Sessions (PTY)
    def session_row(sid):
        row=db.one("SELECT s.*,w.worktree_path,w.agent workspace_agent FROM agent_sessions s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.id=?",(sid,))
        if not row: raise HTTPException(404,"Session not found")
        return row
    def _deliver_to_session(w,sid,note_exited_without_report=False):
        """Compute the current effective Builder prompt -- the Task's
        intent as usual, or, if the latest review for this workspace is
        FIX_REQUIRED, the repair prompt with findings appended (exactly
        what workspace_agent_prompt() already composes, section 4C) --
        and deliver it into a LIVE session's stdin. Always snapshots a
        `prompts` audit row first, regardless of whether delivery itself
        succeeds, so the exact text that was (attempted to be) sent is
        always recoverable. Returns True only on confirmed delivery.
        `note_exited_without_report` is decided by the CALLER before this
        new session ever existed (section 6) -- workspace_agent_prompt()
        must never re-derive it from "the latest session", because by the
        time this prompt is being computed the new session already is
        the latest one."""
        if not w.get("task_id"): return False
        try:
            t=task_row(w["task_id"]); repo_row=repo(w["repository_id"])
            prompt=workspace_agent_prompt(w,t,repo_row,note_exited_without_report=note_exited_without_report)
            review=decision.latest_review(w["id"])
            source="REPAIR" if review and review["status"]=="FIX_REQUIRED" else "TASK"
            db.execute("INSERT INTO prompts(task_id,workspace_id,prompt_type,brief_version,content) VALUES(?,?,?,?,?)",
                       (w["task_id"],w["id"],"BUILDER",t["brief_version"],prompt))
            return agent_sessions.deliver_prompt(sid,prompt,source,t["brief_version"])
        except Exception:
            return False  # snapshot/delivery is best-effort audit, never raises into the caller
    def _start_builder_session(w,mode="INTERACTIVE",note_exited_without_report=False):
        """The one place 'Start Builder' actually happens: validates
        (Task exists, Builder Workspace exists, worktree path is the
        trusted server-resolved one, agent is in the trusted launcher
        registry), starts the AgentSession, then WAITS for the CLI to be
        ready and delivers the exact generated Builder Prompt into it
        (section 1) -- deliberately does NOT check implementation_prompt
        at all; Task Title fallback means intent is always resolvable.
        The user is never expected to paste the prompt manually as the
        normal flow; a failed delivery is recorded as prompt_status
        FAILED for [Retry Prompt Delivery], not silently retried.

        Spec Layer (S5): this is the Supervisor entry point -- the one
        place an Agent session actually starts for a Builder Workspace
        (create_session/_resume_builder_session/start_all_builders all
        go through here). SpecGate runs first; a behavior-changing Task
        (spec_change_classification set to anything but NULL/
        NO_BEHAVIOR_CHANGE) whose spec linkage doesn't PASS never gets
        an Agent started. A Task with no classification at all
        (NOT_APPLICABLE) is unaffected -- every existing/legacy workflow
        keeps working exactly as before this feature existed.

        Role & Capability Catalog (E2): every Builder Workspace launch
        through this function is, by definition, the BUILDER engineering
        role -- there is no separate stored "role" field for this (never
        confuse with agent_workspaces.role, a free-text component/team
        label). codex/claude are seeded SUPPORTED for BUILDER's required
        capabilities, so this is a no-op for every workflow that already
        works; it only turns an already-failing case (an agent name with
        no real launcher, e.g. gemini/aider/other -- previously only
        caught later as a generic AGENT_UNSUPPORTED SessionError) into an
        earlier, clearer message. Never blocks on a catalog/policy
        lookup failure itself (fail-open) -- a broken PROJECT.yaml
        engineering: block or catalog bug must never take down Start."""
        if w["agent"] not in settings.agents: raise GitSafetyError("Agent is not allowed")
        try:
            policy=load_engineering_policy(Path(w["repo_path"])) if w.get("repo_path") else None
            assignment=roles_catalog.validate_assignment(w["agent"],"BUILDER",policy)
        except Exception:
            assignment=None
        if assignment is not None and not assignment["valid"]:
            db.event("agent",w["id"],"ROLE_ASSIGNMENT_REJECTED",
                      f"provider={w['agent']} role=BUILDER missing={assignment['missing_required_capabilities']} policy_blocked={assignment['policy_blocked']}")
            reason=assignment["warnings"][0] if assignment["policy_blocked"] and assignment["warnings"] else \
                f"missing required capabilities: {', '.join(assignment['missing_required_capabilities'])}"
            raise GitSafetyError(f"Cannot assign provider '{w['agent']}' as BUILDER -- {reason}")
        elif assignment is not None:
            db.event("agent",w["id"],"ROLE_ASSIGNMENT_VALIDATED",f"provider={w['agent']} role=BUILDER")
        if w.get("task_id"):
            t=task_row(w["task_id"])
            gate=spec_gate.evaluate(t)
            if gate["outcome"] not in ("PASS","NOT_APPLICABLE"):
                raise GitSafetyError(f"SpecGate {gate['outcome']}: {gate['reason']}")
        mode="VIEW_ONLY" if mode=="VIEW_ONLY" else "INTERACTIVE"
        sid=agent_sessions.start(task_id=w["task_id"],workspace_id=w["id"],agent=w["agent"],worktree_path=w["worktree_path"],mode=mode)
        db.event("agent",w["id"],"SESSION_STARTED",f"session={sid} mode={mode}")
        delivered=_deliver_to_session(w,sid,note_exited_without_report=note_exited_without_report)
        db.event("agent",w["id"],"PROMPT_DELIVERED" if delivered else "PROMPT_DELIVERY_FAILED",f"session={sid}")
        return sid
    def _resume_builder_session(w):
        """[Resume Agent] (section 4): never blindly starts a second
        session on top of one already doing real work.
        A. no live session, or one that already exited -- start a fresh
           one (this doubles as the FIX_REQUIRED repair path, since
           _deliver_to_session already appends review findings then). If
           the prior session EXITED without ever producing a report at
           the current HEAD (section 6/8: EXITED is not itself failure),
           the fresh session's prompt gets a focused note about that --
           decided HERE, before the new session exists, never re-derived
           from "the latest session" downstream.
        B. a live session whose prompt was already DELIVERED -- the
           agent is presumably still working; no-op, never resend the
           original task a second time.
        C. a live session that never got its prompt delivered (PENDING/
           FAILED) -- deliver into that SAME session, no duplicate
           process."""
        session=decision.latest_session(w["id"])
        if session and session["status"] in ("STARTING","RUNNING","WAITING_FOR_INPUT"):
            if session["prompt_status"] in ("PENDING","FAILED"):
                delivered=_deliver_to_session(w,session["id"])
                db.event("agent",w["id"],"PROMPT_DELIVERED" if delivered else "PROMPT_DELIVERY_FAILED",f"session={session['id']} (retry)")
            return session["id"]
        note_exited=bool(session and session["status"] in ("EXITED","FAILED") and w["status"]!="READY")
        return _start_builder_session(w,note_exited_without_report=note_exited)
    @app.post("/api/workspaces/{wid}/sessions")
    def create_session(wid:int,mode:str=Form("INTERACTIVE"), _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        """Command safety (section 14): the browser supplies only
        workspace_id + (fixed) mode -- agent name and cwd are both
        resolved server-side from the trusted workspace/launcher
        registry, never taken from the request. Duplicate-click
        protection (button-feedback section 4/8): a second 'Start
        Builder' click while a session is already STARTING/RUNNING/
        WAITING_FOR_INPUT reuses that live session (via
        _resume_builder_session's own guard) instead of forking a second
        real pty process for the same workspace."""
        w=agent_row(wid)
        try: sid=_resume_builder_session(w) if mode=="INTERACTIVE" else _start_builder_session(w,mode)
        except SessionError as exc: raise GitSafetyError(str(exc)) from exc
        return RedirectResponse(f"/workspaces/{wid}/sessions/{sid}",303)
    @app.post("/api/sessions/{sid}/deliver-prompt")
    def retry_prompt_delivery(sid:int, _authz: None = Depends(require_role("agent_session", "sid", "MEMBER"))):
        """[Retry Prompt Delivery] (section 3): the explicit, exceptional
        recovery action for a session whose automatic delivery failed --
        never triggered automatically on a timer/poll."""
        s=session_row(sid); w=agent_row(s["workspace_id"])
        delivered=_deliver_to_session(w,sid)
        db.event("agent",w["id"],"PROMPT_DELIVERED" if delivered else "PROMPT_DELIVERY_FAILED",f"session={sid} (manual retry)")
        return RedirectResponse(f"/workspaces/{w['id']}/sessions/{sid}",303)
    @app.post("/api/tasks/{tid}/start-all-builders")
    def start_all_builders(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """[Start All Builders]: for every valid Builder Workspace that
        isn't already RUNNING (agent_status not in the live-session set),
        start its AgentSession -- a missing Implementation Prompt never
        blocks any of them (Task Title fallback), and an already-running
        one is skipped, never restarted."""
        task_row(tid)
        d=decision.evaluate(tid)
        for b in d["builders"]:
            if b["agent_status"] in ("STARTING","RUNNING","WAITING_FOR_INPUT"): continue
            if b["agent"] not in settings.agents: continue
            w=agent_row(b["id"])
            if w["status"]=="CLOSED": continue
            try: _start_builder_session(w)
            except SessionError as exc: db.event("agent",b["id"],"SESSION_START_FAILED",str(exc))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/sessions/{sid}/stop")
    def stop_session(sid:int, _authz: None = Depends(require_role("agent_session", "sid", "MEMBER"))):
        session_row(sid); agent_sessions.stop(sid); return RedirectResponse(f"/workspaces/{session_row(sid)['workspace_id']}",303)
    @app.post("/api/sessions/{sid}/mode")
    def set_session_mode(sid:int,mode:str=Form(...), _authz: None = Depends(require_role("agent_session", "sid", "MEMBER"))):
        """The actual VIEW_ONLY/INTERACTIVE security boundary: persisted
        here and re-read by the WebSocket handler on every stdin message,
        never trusted from anything the client itself claims."""
        session_row(sid)
        if mode not in ("INTERACTIVE","VIEW_ONLY"): raise GitSafetyError("Invalid mode")
        agent_sessions.set_mode(sid,mode)
        return {"ok":True,"mode":mode}
    @app.get("/agents/live",response_class=HTMLResponse)
    def agents_live(request:Request):
        rows=_filter_rows(db.all("SELECT s.*,w.agent workspace_agent,w.role,w.repository_id,r.repo_name,w.task_id tid,t.title task_title FROM agent_sessions s "
                     "JOIN agent_workspaces w ON w.id=s.workspace_id JOIN repositories r ON r.id=w.repository_id LEFT JOIN tasks t ON t.id=w.task_id "
                     "WHERE s.status IN ('STARTING','RUNNING','WAITING_FOR_INPUT') ORDER BY s.last_activity_at DESC"),_visible_repo_ids(request),"repository_id")
        for row in rows:
            sb=sandbox_for_workspace(row["workspace_id"]); row["sandbox_status"]=sb["status"] if sb else None; row["sandbox_health"]=sb["health_status"] if sb else None
            row["activity_hint"]=activity_summary(row["id"], 160)
        return render(request,"agents_live.html",sessions=rows)
    @app.get("/workspaces/{wid}/sessions/{sid}",response_class=HTMLResponse)
    def session_detail(request:Request,wid:int,sid:int, _authz: None = Depends(require_read_role("workspace", "wid"))):
        w=agent_row(wid); s=session_row(sid)
        if s["workspace_id"]!=wid: raise HTTPException(404)
        t=task_row(w["task_id"]) if w.get("task_id") else None
        return render(request,"session_detail.html",w=w,s=s,t=t)
    @app.websocket("/ws/sessions/{sid}")
    async def session_ws(websocket:WebSocket,sid:int):
        """Browser <-> WebSocket <-> real PTY. VIEW_ONLY connections never
        forward stdin, enforced here server-side regardless of what the
        client sends (section 15)."""
        await websocket.accept()
        row=db.one("SELECT * FROM agent_sessions WHERE id=?",(sid,))
        live=agent_sessions.get(sid)
        if not row or not live:
            await websocket.send_json({"type":"exit","exit_code":row["exit_code"] if row else None,"status":row["status"] if row else "NOT_FOUND"})
            await websocket.close(); return
        loop=asyncio.get_event_loop()
        def send_chunk(chunk:bytes):
            try: asyncio.run_coroutine_threadsafe(websocket.send_bytes(chunk),loop)
            except RuntimeError: pass
        tail=live.subscribe(send_chunk)
        try:
            if tail: await websocket.send_bytes(tail)
            while True:
                msg=await websocket.receive()
                if msg.get("type")=="websocket.disconnect": break
                if "text" in msg and msg["text"] is not None:
                    try: data=json.loads(msg["text"])
                    except ValueError: continue
                    if data.get("type")=="resize": live.resize(int(data.get("rows",24)),int(data.get("cols",80)))
                elif "bytes" in msg and msg["bytes"] is not None:
                    if live.mode=="INTERACTIVE" and not live.closed:
                        try: live.write(msg["bytes"])
                        except SessionError: pass
        except WebSocketDisconnect:
            pass
        finally:
            live.unsubscribe(send_chunk)
            try: agent_sessions.persist_tail(sid)
            except Exception: pass

    @app.post("/api/workspaces/{wid}/open-terminal")
    def open_terminal(wid:int, _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        w=agent_row(wid)
        try:
            result=launcher.open_terminal(w["worktree_path"]); db.event("agent",wid,"TERMINAL_OPENED",f"agent={w['agent']} path={result['worktree']} terminal={result['terminal']} result=requested")
            return {"ok":True,"message":"Terminal đã được yêu cầu mở đúng worktree.",**result}
        except LauncherError as exc:
            db.event("agent",wid,"AGENT_LAUNCH_FAILED",f"agent={w['agent']} path={w['worktree_path']} result={exc.code}")
            return JSONResponse({"ok":False,"code":exc.code,"message":str(exc),"fallback":f"cd {w['worktree_path']}"},status_code=409)
    @app.post("/api/workspaces/{wid}/launch-agent")
    def launch_agent(wid:int, _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        """This spawns a REAL, independent codex/claude process in a
        desktop terminal window on THIS HOST's own graphical session
        (TerminalLauncherService, entirely separate from AgentSession's
        browser/PTY mechanism) -- invisible to the app's own state
        tracking (agent_status, review readiness, ...) once running.
        Workspace Detail UX audit (section 3): no longer exposed as a
        page button -- the one real live-agent flow is AgentSession
        (Open Live Agent / Start Codex in the Current Action block).
        The route stays for API compatibility, but the SAME concurrency
        guard applies: never spawn a second, untracked agent process on
        top of one already doing real, tracked work."""
        w=agent_row(wid)
        live=latest_session_for_workspace(wid)
        if live and live["status"] in LIVE_SESSION_STATUSES:
            return JSONResponse({"ok":False,"code":"ACTIVE_SESSION_EXISTS","message":f"An AgentSession is already {live['status']} for this workspace -- open it instead of starting a second, untracked agent process.","session_id":live["id"]},status_code=409)
        try:
            result=launcher.launch_agent(w["worktree_path"],w["agent"]); db.event("agent",wid,"AGENT_LAUNCHED",f"agent={w['agent']} path={result['worktree']} terminal={result['terminal']} result=requested")
            return {"ok":True,"message":f"{result['agent']} terminal đã được yêu cầu mở.",**result}
        except LauncherError as exc:
            db.event("agent",wid,"AGENT_LAUNCH_FAILED",f"agent={w['agent']} path={w['worktree_path']} result={exc.code}")
            return JSONResponse({"ok":False,"code":exc.code,"message":str(exc),"fallback":f"cd {w['worktree_path']}"},status_code=409)

    @app.get("/integrations",response_class=HTMLResponse)
    def integrations(request:Request):
        repo_ids=_visible_repo_ids(request)
        return render(request,"integrations.html",
                       integrations=_filter_rows(db.all("SELECT i.*,r.repo_name FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id ORDER BY i.updated_at DESC"),repo_ids,"repository_id"),
                       repositories=_filter_rows(db.all("SELECT * FROM repositories WHERE enabled=1"),repo_ids),
                       agents=_filter_rows(db.all("SELECT w.*,r.repo_name FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.status IN ('READY','CODING','CREATED')"),repo_ids,"repository_id"))
    @app.post("/api/integrations")
    async def create_integration(request:Request, _csrf: None = Depends(_mutating_csrf)):
        form=await request.form(); repository_id=int(form["repository_id"]); name=slugify(str(form["name"])); base=str(form.get("base_branch","main")); ids=[int(x) for x in form.getlist("workspace_ids")]
        _require_org_role_for_repo(request, repository_id, "MEMBER")
        if not ids: raise GitSafetyError("Select at least one agent workspace")
        r=repo(repository_id); sources=[agent_row(x) for x in ids]
        if any(x["repository_id"]!=repository_id for x in sources): raise GitSafetyError("All sources must belong to integration repository")
        branch,path,commit=git.create_integration(r["repo_path"],name,base)
        iid=db.execute("INSERT INTO integration_workspaces(repository_id,name,branch,worktree_path,base_branch,base_commit,status) VALUES(?,?,?,?,?,?,?)",(repository_id,name,branch,str(path),base,commit,"MERGING")); db.event("integration",iid,"INTEGRATION_CREATED",branch)
        for source in sources:
            result=git.merge(path,source["branch"]); current=git.head(r["repo_path"],source["branch"])
            db.execute("INSERT INTO integration_sources(integration_id,workspace_id,merged_commit,merged_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",(iid,source["id"],current))
            if result.returncode:
                db.execute("UPDATE integration_workspaces SET status='CONFLICT' WHERE id=?",(iid,)); db.event("integration",iid,"MERGE_CONFLICT",source["branch"]); break
            db.event("integration",iid,"BRANCH_MERGED",source["branch"])
        else: db.execute("UPDATE integration_workspaces SET status='TESTING' WHERE id=?",(iid,))
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.get("/api/integrations")
    def api_integrations(request: Request): return _filter_rows(db.all("SELECT * FROM integration_workspaces"),_visible_repo_ids(request),"repository_id")
    @app.get("/integrations/{iid}",response_class=HTMLResponse)
    def integration_detail(request:Request,iid:int, _authz: None = Depends(require_read_role("integration", "iid"))):
        i=integration_row(iid); sources=db.all("SELECT s.*,w.agent,w.task_name,w.branch,w.worktree_path FROM integration_sources s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.integration_id=?",(iid,)); stale=[]
        for s in sources:
            current=git.head(i["repo_path"],s["branch"]); s["current_commit"]=current; s["stale"]=current!=s["merged_commit"]; stale.append(s["stale"])
        head=git.head(i["worktree_path"]); readiness_stale=bool(i["verified_commit"] and i["verified_commit"]!=head) or any(stale)
        if readiness_stale and i["ready_for_main"]: invalidate(iid); i=integration_row(iid)
        # Push state (section 5/6/11): local HEAD is always read live
        # above; "actually pushed" means the LAST successful push's HEAD
        # still matches it -- a stale push_status='PUSHED' from before a
        # later local commit is treated as not-pushed, never trusted.
        conflicts=git.conflict_files(i["worktree_path"]); dirty=bool(git.status(i["worktree_path"]).strip())
        pushed=bool(i["last_pushed_head"]) and i["last_pushed_head"]==head and i["push_status"]=="PUSHED"
        github_available=github_merge.available(i["repo_path"])
        push_disabled_reason=None
        if not github_available: push_disabled_reason="Repository has no GitHub remote."
        elif conflicts: push_disabled_reason="Integration is still in conflict."
        elif dirty: push_disabled_reason="Commit or resolve local changes first."
        elif pushed: push_disabled_reason="Already pushed at this exact HEAD."
        pr=None
        ti_row=db.one("SELECT task_id FROM task_integrations WHERE id=?",(i["task_integration_id"],)) if i["task_integration_id"] else None
        task_id=ti_row["task_id"] if ti_row else None
        if ti_row:
            pr=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(task_id,i["repository_id"]))
        gate_status=decision.integration_gate_status(i,task_id)
        # Sections 17-20: exactly one dominant primary action, derived
        # from the SAME authoritative ladder the Task wizard uses
        # (TaskDecisionService.integration_next_action) -- never a
        # separately-hardcoded ordering here. A stale/unmerged source
        # branch is checked first since the decision-engine ladder itself
        # has no notion of `integration_sources` (that table is purely a
        # git-worktree/Integration-page concern, section 1's Merge Latest
        # Changes), and an in-progress conflict always wins outright.
        i["gate_status"]=gate_status
        if any(s["stale"] for s in sources) and i["status"]!="CONFLICT":
            primary_action={"action":"MERGE_LATEST"}
        else:
            primary_action=decision.integration_next_action(i,task_id,pr)
            # Section 19: never present PUSH_INTEGRATION as the dominant
            # action when it is not actually reachable (no GitHub remote
            # configured at all) -- ready-for-main itself never requires
            # push_status (only clean/no-conflicts/sources-current/tests-
            # pass), so once tests pass a GitHub-less repo's real next
            # step is Confirm Ready for Main, not a Push button that
            # would just 409 if clicked.
            if primary_action["action"]=="PUSH_INTEGRATION" and not github_available:
                primary_action=dict(primary_action,action="CONFIRM_INTEGRATION_READY")
        return render(request,"integration_detail.html",i=i,sources=sources,conflicts=conflicts,head=head,stale=readiness_stale,
                      runs=db.all("SELECT * FROM test_runs WHERE workspace_type='integration' AND workspace_id=? ORDER BY id DESC",(iid,)),
                      events=db.all("SELECT * FROM workspace_events WHERE entity_type='integration' AND entity_id=? ORDER BY id",(iid,)),
                      pushed=pushed,push_disabled_reason=push_disabled_reason,pr=pr,gate_status=gate_status,
                      primary_action=primary_action["action"],primary_action_label=primary_action.get("label"),
                      current_action=integration_current_action(iid),
                      merge_latest_op=ops.latest("integration",iid,"MERGE_LATEST"),
                      push_op=ops.latest("integration",iid,"PUSH_INTEGRATION"),
                      ready_op=ops.latest("integration",iid,"MARK_READY_FOR_MAIN"))
    @app.post("/api/integrations/{iid}/merge-latest")
    def merge_latest(iid:int, _authz: None = Depends(require_role("integration", "iid", "MEMBER"))):
        """[Merge Latest Changes]: tracked as one MERGE_LATEST Operation
        (button-feedback section 8) -- a second click while one is still
        RUNNING is reflected back, never launches a second real git merge
        (section 4)."""
        i=integration_row(iid)
        if git.conflict_files(i["worktree_path"]): raise GitSafetyError("Resolve current conflicts before merging latest")
        try: op_id=ops.begin("integration",iid,"MERGE_LATEST")
        except OperationInProgress: return RedirectResponse(f"/integrations/{iid}",303)
        try:
            sources=db.all("SELECT s.*,w.branch FROM integration_sources s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.integration_id=?",(iid,)); invalidate(iid)
            merged_any=False; conflict_branch=None
            for s in sources:
                current=git.head(i["repo_path"],s["branch"])
                if current==s["merged_commit"]: continue
                result=git.merge(i["worktree_path"],s["branch"])
                if result.returncode:
                    db.execute("UPDATE integration_workspaces SET status='CONFLICT' WHERE id=?",(iid,)); db.event("integration",iid,"MERGE_CONFLICT",s["branch"])
                    conflict_branch=s["branch"]; break
                db.execute("UPDATE integration_sources SET merged_commit=?,merged_at=CURRENT_TIMESTAMP WHERE integration_id=? AND workspace_id=?",(current,iid,s["workspace_id"])); db.event("integration",iid,"BRANCH_MERGED",s["branch"])
                merged_any=True
            if conflict_branch: ops.fail(op_id,f"Conflict detected in {conflict_branch}")
            else: ops.succeed(op_id,"Merged latest changes" if merged_any else "Already up to date")
        except Exception as exc:
            ops.fail(op_id,exc); raise
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/test")
    def test_integration(iid:int, _authz: None = Depends(require_role("integration", "iid", "MEMBER"))):
        """[Run Tests]: test_runs already tracks QUEUED/RUNNING/PASS/FAIL
        (TestRunner runs it in a background thread) -- reused as-is
        rather than a second `operations` row (db.py V12 comment). The
        only gap closed here is duplicate-click protection: a click while
        the current run for this Integration is still QUEUED/RUNNING is
        a no-op, never a second concurrent TestRun (section 4).
        A repo with no PROJECT.yaml (or one declaring zero required CI
        stages) has nothing for TestRunner to queue -- integration_gate_
        status() already resolves that straight to PASS, so a click here
        is a no-op back to the same page rather than a raw ContractError
        (real incident: this used to 500)."""
        i=integration_row(iid)
        if git.conflict_files(i["worktree_path"]): raise GitSafetyError("Cannot test unresolved conflicts")
        active=db.one("SELECT id FROM test_runs WHERE workspace_type='integration' AND workspace_id=? AND status IN ('QUEUED','RUNNING') ORDER BY id DESC LIMIT 1",(iid,))
        if active: return RedirectResponse(f"/integrations/{iid}",303)
        invalidate(iid)
        try: runner.start("integration",iid,Path(i["worktree_path"]))
        except ContractError: pass
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/ready-for-main")
    def ready_main(iid:int, _authz: None = Depends(require_role("integration", "iid", "MEMBER"))):
        """[Mark Ready for Main] (section 15): tracked as one
        MARK_READY_FOR_MAIN Operation so the button never looks unchanged
        after a click -- 'Validating readiness...' while RUNNING (this
        check is local/fast, so in practice RUNNING is only ever visible
        to a concurrent second click or a very unlucky refresh), then
        'Ready for Main' or 'Blocked: <exact reason>'."""
        try: op_id=ops.begin("integration",iid,"MARK_READY_FOR_MAIN")
        except OperationInProgress: return RedirectResponse(f"/integrations/{iid}",303)
        try:
            r=integration_readiness(iid)
            if not r["clean"]: raise GitSafetyError("Integration worktree must be clean")
            if not r["no_conflicts"]: raise GitSafetyError("Merge conflict exists")
            if not r["sources_current"]: raise GitSafetyError("Source not current")
            if not r["tests_pass"]: raise GitSafetyError("All required tests must PASS at current HEAD")
            db.execute("UPDATE integration_workspaces SET status='READY_FOR_MAIN',ready_for_main=1,verified_commit=?,verified_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(r["head"],iid)); db.event("integration",iid,"READY_FOR_MAIN",r["head"])
            ops.succeed(op_id,f"Ready for Main at {r['head'][:8]}")
        except Exception as exc:
            ops.fail(op_id,exc); raise
        return RedirectResponse(f"/integrations/{iid}",303)
    INTEGRATION_PUSH_BRANCH_DENYLIST={"main","master","production"}
    @app.post("/api/integrations/{iid}/push")
    def push_integration(iid:int, _authz: None = Depends(require_role("integration", "iid", "MEMBER"))):
        """[Push Integration Branch] (section 1-3): the ONLY way an
        Integration branch's local HEAD reaches GitHub -- a real,
        non-force `git push origin <branch>:<branch>`, derived entirely
        from the registered Integration record (repo path, worktree,
        branch) -- never a branch/remote/cwd typed in the browser.
        Reuses GitHubMergeService.push_branch(), the same primitive
        Create PR already uses, rather than a second ad-hoc subprocess
        call (section 2). Refuses on a dirty worktree, an unresolved
        conflict, the worktree somehow not being on the integration
        branch, or the branch being main/master/production."""
        i=integration_row(iid)
        if i["branch"] in INTEGRATION_PUSH_BRANCH_DENYLIST:
            raise GitSafetyError("Refusing to push a main/master/production branch as an Integration branch")
        if git.conflict_files(i["worktree_path"]):
            raise GitSafetyError("INTEGRATION_WORKTREE_DIRTY: unresolved merge conflict -- resolve before pushing")
        if git.status(i["worktree_path"]).strip():
            raise GitSafetyError("INTEGRATION_WORKTREE_DIRTY: worktree has uncommitted changes")
        try: head=git.head(i["worktree_path"])
        except Exception as exc: raise GitSafetyError(f"HEAD does not resolve: {exc}") from exc
        current_branch=git.git(i["worktree_path"],"rev-parse","--abbrev-ref","HEAD",check=False).stdout.strip()
        if current_branch!=i["branch"]:
            raise GitSafetyError(f"INTEGRATION_BRANCH_MISMATCH: worktree is on '{current_branch}', expected '{i['branch']}'")
        if not github_merge.available(i["repo_path"]):
            raise GitSafetyError("REMOTE_NOT_CONFIGURED: repository has no GitHub remote")
        try: op_id=ops.begin("integration",iid,"PUSH_INTEGRATION")
        except OperationInProgress: return RedirectResponse(f"/integrations/{iid}",303)
        db.event("integration",iid,"INTEGRATION_PUSH_STARTED",f"branch={i['branch']} local_head={head}")
        try:
            github_merge.push_branch(i["repo_path"],i["branch"])
        except GitHubIntegrationError as exc:
            msg=str(exc).lower()
            blocked=any(s in msg for s in ("non-fast-forward","fetch first","rejected","stale info"))
            db.execute("UPDATE integration_workspaces SET push_status=?,push_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       ("PUSH_BLOCKED_REMOTE_CHANGED" if blocked else "PUSH_FAILED",str(exc)[:2000],iid))
            db.event("integration",iid,"INTEGRATION_PUSH_FAILED",f"branch={i['branch']} error={exc}")
            ops.fail(op_id,exc)
            raise GitSafetyError(f"{'PUSH_BLOCKED_REMOTE_CHANGED' if blocked else 'PUSH_FAILED'}: {exc}") from exc
        db.execute("UPDATE integration_workspaces SET last_pushed_head=?,push_status='PUSHED',pushed_at=CURRENT_TIMESTAMP,push_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,iid))
        db.event("integration",iid,"INTEGRATION_PUSH_SUCCEEDED",f"branch={i['branch']} head={head}")
        pr_note=""
        # Section 9/10: refresh the SAME existing PR if one exists for
        # this repo on this Task -- NEVER create a new one from here.
        ti=db.one("SELECT task_id FROM task_integrations WHERE id=?",(i["task_integration_id"],)) if i["task_integration_id"] else None
        if ti:
            mr=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(ti["task_id"],i["repository_id"]))
            if mr and mr["pr_number"]:
                try:
                    # Real, observed behavior: GitHub's PR headRefOid can
                    # very briefly lag right after a push completes --
                    # a small bounded retry (never indefinite) so "push
                    # refreshes the PR" is actually reliable, not racy.
                    status=github_merge.pr_status(i["repo_path"],mr["pr_number"])
                    for _ in range(4):
                        if status["head_sha"]==head: break
                        time.sleep(0.5)
                        status=github_merge.pr_status(i["repo_path"],mr["pr_number"])
                    db.execute(
                        "UPDATE merge_records SET pr_state=?,ci_status=?,mergeability=?,merge_state_status=?,head_sha=?,last_synced_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (status["pr_state"],status["ci_status"],status["mergeability"],status["merge_state_status"],status["head_sha"],mr["id"]))
                    pr_note=f" -- PR #{mr['pr_number']} updated"
                except GitHubIntegrationError:
                    pass  # the push itself already succeeded; PR-status refresh is best-effort
        ops.succeed(op_id,f"Pushed {head[:8]}{pr_note}")
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/reproduce-baseline")
    def reproduce_baseline_failure(iid:int,gate:str=Form(...),test_identifier:str=Form(...), _authz: None = Depends(require_role("integration", "iid", "MEMBER"))):
        """[View Baseline Evidence] step 1 (section 12/22): actually
        reproduces the failure against this Integration's own recorded
        base_commit in a disposable, detached worktree -- the only way a
        BaselineFailureEvidence row is ever written. Runs in the
        background (a real gate command can take minutes); the route
        returns immediately, the Integration step polls/shows the
        resulting evidence once it lands."""
        i=integration_row(iid)
        try: run_id=gate_waivers.start_reproduction(repository_id=i["repository_id"],repo_path=i["repo_path"],base_commit=i["base_commit"],gate=gate,test_identifier=test_identifier)
        except GateWaiverError as exc: raise GitSafetyError(str(exc)) from exc
        db.event("integration",iid,"BASELINE_REPRODUCTION_STARTED",f"gate={gate} test={test_identifier} run={run_id}")
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/waive-baseline-failure")
    def waive_baseline_failure(iid:int,gate:str=Form(...),test_identifier:str=Form(...),reason:str=Form(""), _authz: None = Depends(require_role("integration", "iid", "MEMBER"))):
        """[Waive Baseline Failure] (section 14-17): the ONLY way past a
        currently-failing required gate other than actually fixing it --
        and only for a failure this route independently re-classifies
        (via the exact same TaskDecisionService.integration_gate_status()
        the Integration step itself displays) as BASELINE_FAILURE right
        now, at the current fingerprint. Never a blanket 'ignore tests':
        a mismatched or already-resolved failure is refused, not silently
        accepted. A waiver never marks anything PASS -- it only makes
        this one exact (gate, test, fingerprint) not block Ready for
        Main, via PASS_WITH_APPROVED_BASELINE_WAIVER."""
        i=integration_row(iid)
        ti=db.one("SELECT task_id FROM task_integrations WHERE id=?",(i["task_integration_id"],)) if i["task_integration_id"] else None
        if not ti: raise GitSafetyError("Integration has no parent Task")
        gs=decision.integration_gate_status(i,ti["task_id"])
        match=next((f for f in gs["failures"] if f["stage"]==gate and f["test_identifier"]==test_identifier),None)
        if not match: raise GitSafetyError("No matching current failure to waive at this exact commit")
        if match["classification"]!="BASELINE_FAILURE": raise GitSafetyError(f"Not eligible for a baseline waiver (classification={match['classification']})")
        evidence=db.one("SELECT * FROM baseline_failure_evidence WHERE id=?",(match["evidence_id"],))
        gate_waivers.approve_waiver(task_id=ti["task_id"],integration_id=iid,gate=gate,test_identifier=test_identifier,
                                     failure_fingerprint_value=match["fingerprint"],baseline_commit=evidence["base_commit"],
                                     baseline_run_id=evidence["baseline_run_id"],integration_run_id=None,
                                     reason=reason.strip() or "Verified pre-existing baseline failure, unrelated to this Task.",
                                     approved_by="local-operator")
        db.event("integration",iid,"BASELINE_WAIVER_APPROVED",f"gate={gate} test={test_identifier}")
        return RedirectResponse(f"/tasks/{ti['task_id']}",303)
    @app.post("/api/integrations/{iid}/close")
    def close_integration(iid:int, _authz: None = Depends(require_role("integration", "iid", "MEMBER"))): i=integration_row(iid); git.close(i["repo_path"],i["worktree_path"]); db.execute("UPDATE integration_workspaces SET status='CLOSED',ready_for_main=0,closed_at=CURRENT_TIMESTAMP WHERE id=?",(iid,)); db.event("integration",iid,"WORKSPACE_CLOSED"); return RedirectResponse("/integrations",303)

    @app.get("/test-runs",response_class=HTMLResponse)
    def runs(request:Request): return render(request,"test_runs.html",runs=_filter_polymorphic(request,"test_run",db.all("SELECT * FROM test_runs ORDER BY id DESC LIMIT 200")))
    @app.get("/api/test-runs/{rid}")
    def api_run(rid:int, _authz: None = Depends(require_read_role("test_run", "rid"))):
        row=db.one("SELECT * FROM test_runs WHERE id=?",(rid,));
        if not row: raise HTTPException(404)
        return row
    @app.get("/api/operations/{op_id}")
    def api_operation(op_id:int, _authz: None = Depends(require_read_role("operation", "op_id"))):
        """Polling endpoint for the generic action-button feedback model
        (Merge Latest Changes / Push Integration Branch / Create PR /
        Merge PR / Mark Ready for Main). The button-feedback JS polls
        this while status is QUEUED/RUNNING and reloads the page once it
        reaches a terminal state, so the page always re-renders from the
        real persisted result -- never a client-side guess."""
        row=db.one("SELECT * FROM operations WHERE id=?",(op_id,))
        if not row: raise HTTPException(404)
        return row
    @app.get("/test-runs/{rid}/log",response_class=PlainTextResponse)
    def run_log(rid:int, _authz: None = Depends(require_read_role("test_run", "rid"))):
        row=db.one("SELECT * FROM test_runs WHERE id=?",(rid,));
        if not row: raise HTTPException(404)
        return f"STDOUT (tail)\n{row['stdout_tail']}\n\nSTDERR (tail)\n{row['stderr_tail']}"
    @app.get("/settings",response_class=HTMLResponse)
    def settings_page(request:Request): return render(request,"settings.html",launchers=launcher.status())

    # ---------------------------------------------------------------- Tasks
    def recompute_task_status(tid):
        """No-op (section 31/32): TaskDecisionService.evaluate() now
        derives ACTIVE/BLOCKED/READY_FOR_MAIN/DONE live from real
        workspace/review/QA/integration/merge state on every read. Only
        BACKLOG/ACTIVE/CANCELLED are ever persisted to tasks.status, and
        those change only through explicit user actions (select/cancel) --
        never something Submit for Review should silently overwrite. Kept
        as a no-op so its existing call sites (e.g. ready()) don't need to
        change."""
        return

    def resolve_runtime_dependency_sources(contract):
        """Section 4/5 of the QA Center sandbox spec: a sandbox: contract
        can declare `runtime_dependencies` (e.g. qa-center -> mesflow-app,
        mode KNOWN_GOOD_MAIN) -- modeled as real, separately-labeled
        RUNTIME_DEPENDENCY sandbox_sources rows, never a second Builder
        Workspace and never a browser-supplied path. The dependency's
        repo/path always comes from ProjectFlow's own trusted
        `repositories` registry (the exact same resolution
        check_dependency_cycle()/repository_contract_editor already use
        for this same field) -- an undeclared/unregistered repo name is
        silently skipped, never guessed. KNOWN_GOOD_MAIN prefers that
        repo's latest DEV deployment's exact pinned commit (what a human
        would mean by 'known good') and falls back to the repo's own
        current registered HEAD if no DEV deployment is tracked yet --
        never PRODUCTION, never a secret, never mesflow.net (section 19)."""
        sources=[]
        for dep in (contract.get("runtime_dependencies") or []):
            name=(dep.get("repo") or "").strip()
            if not name: continue
            row=db.one("SELECT * FROM repositories WHERE repo_name=? AND enabled=1",(name,))
            if not row: continue
            mode=(dep.get("mode") or "KNOWN_GOOD_MAIN").strip()
            dep_deployment=None
            if mode=="KNOWN_GOOD_MAIN":
                dep_deployment=db.one("SELECT * FROM deployments WHERE repository_id=? AND environment='DEV' AND status='VERIFIED' ORDER BY id DESC LIMIT 1",(row["id"],))
            if dep_deployment:
                branch=dep_deployment["source_branch"]; commit_sha=dep_deployment["source_commit"]
            else:
                try: branch="main"; commit_sha=git.head(row["repo_path"])
                except Exception: continue  # repo unreachable -- never fabricate a commit
            sources.append(SourceSpec(repository_id=row["id"],role=name,branch=branch,commit_sha=commit_sha,worktree_path=row["repo_path"],repo_path=row["repo_path"],source_type="RUNTIME_DEPENDENCY"))
        return sources
    def auto_create_sandbox(task_id,repository_id,repo_path,owner_type,owner_id,role,branch,commit,worktree_path,explicit_profile,task_default_profile):
        # Idempotency guard (real incident, same class as the Create
        # Integration duplicate-click bug fixed in #30): a workspace's
        # sandbox is already auto-created at workspace-creation time
        # (add_task_workspace -> here); a Create Sandbox click afterward
        # -- or a second click while the first request is still in
        # flight -- must never create a SECOND sandbox for the same
        # owner. CLOSED (fully torn down) is the only status a fresh one
        # is actually warranted for; TaskDecisionService.
        # builder_sandbox_state() already knows how to route a
        # CLOSED/STOPPED sandbox to Restart/Rebuild instead of Create.
        existing=db.one("SELECT id FROM sandboxes WHERE owner_type=? AND owner_id=? AND status!='CLOSED' ORDER BY id DESC LIMIT 1",(owner_type,owner_id))
        if existing: return existing["id"]
        try: contract=load_sandbox_contract(Path(repo_path))
        except SandboxContractError as exc:
            db.event(owner_type.lower(),owner_id,"SANDBOX_CONTRACT_INVALID",str(exc)); return None
        if contract is None: return None
        profile=resolve_profile(contract,explicit_profile,task_default_profile)
        if profile=="NONE": return None
        source=SourceSpec(repository_id=repository_id,role=role,branch=branch,commit_sha=commit,worktree_path=str(worktree_path),repo_path=str(repo_path),source_type=owner_type)
        extra_sources=resolve_runtime_dependency_sources(contract)
        try:
            sid=sandboxes.create(task_id=task_id,owner_type=owner_type,owner_id=owner_id,profile=profile,provider=source,extra_sources=extra_sources or None)
        except SandboxError as exc:
            db.event(owner_type.lower(),owner_id,"SANDBOX_CREATE_FAILED",str(exc)); return None
        if sid is None: return None
        try: sandboxes.provision(sid)
        except SandboxError: pass  # sandbox row already recorded FAILED; source worktree untouched
        return sid

    KANBAN_COLUMNS=["Backlog","Development","Review / QA","Integration","Ready for Main","Done","Blocked"]
    def workspace_status_label(status):
        """CREATED reads as CODING to a user -- the DB enum stays CREATED/
        READY/CLOSED (no new status column), this is presentation only."""
        return {"CREATED":"CODING"}.get(status,status)
    def kanban_column_for(d):
        """Kanban column, mapped from TaskDecisionService's own computed
        status/stage -- never a second calculation. Section 35's 7 columns
        are coarser than the 6 statuses x 7 stages TaskDecisionService can
        return, so this is a pure lookup, not new logic."""
        if d["status"]=="BLOCKED": return "Blocked"
        if d["status"]=="BACKLOG": return "Backlog"
        if d["status"]=="DONE": return "Done"
        if d["status"]=="READY_FOR_MAIN": return "Ready for Main"
        return {"PLANNING":"Development","DEVELOPMENT":"Development","REVIEW":"Review / QA","QA":"Review / QA",
                "INTEGRATION":"Integration","MERGING":"Integration","COMPLETE":"Done"}.get(d["stage"],"Development")
    def task_card_view(t):
        """Everything one Task card (Kanban or List) needs. status/stage/
        next_action/ready_for_main/blocking_reasons all come from
        TaskDecisionService.evaluate() -- this function only adds the
        lightweight display aggregates (sandbox/test tallies, repo list)
        that decision itself has no reason to compute."""
        d=decision.evaluate(t["id"])
        ws=d["builders"]
        sbxs=task_sandboxes(t["id"])
        # CLEANUP_ELIGIBLE is a retention countdown, not a runtime state --
        # counted as running here too so a just-completed Task's card
        # doesn't misreport its still-live sandbox as not running.
        sandbox={"total":len(sbxs),"running":sum(1 for s in sbxs if s["status"] in ("RUNNING","CLEANUP_ELIGIBLE")),
                 "unhealthy":sum(1 for s in sbxs if s["status"] in ("RUNNING","CLEANUP_ELIGIBLE") and s["health_status"]!="HEALTHY")}
        tests={"passed":sum(1 for w in ws if w["ready"]),"total":len(ws)}
        agents={"total":len(ws),"ready":sum(1 for w in ws if w["ready"]),
                "coding":sum(1 for w in ws if not w["ready"]),"failed":sum(1 for w in ws if w["fix_required"])}
        column=kanban_column_for(d)
        # CSS-safe class for the column badge -- "Review / QA" etc. can't
        # be lowercased straight into a class name.
        column_class={"Backlog":"backlog","Development":"development","Review / QA":"review",
                      "Integration":"integration","Ready for Main":"ready","Done":"done","Blocked":"blocked"}.get(column,"development")
        for w in ws: w["status_label"]=workspace_status_label(w["status"])
        blocking_workspace=next((w for w in ws if w["fix_required"]), next((w for w in ws if not w["ready"]), None))
        return {"task":t,"decision":d,"workspaces":ws,"agents":agents,"sandbox":sandbox,"tests":tests,
                "integration":("NONE" if not d["task_integration"] else d["task_integration"]["status"]),
                "column":column,"column_class":column_class,"blocking_workspace":blocking_workspace,"repos":sorted({w["repo_name"] for w in ws}),
                "next_action":d["next_action"],"needs_fix":bool(d["blocking_reasons"]),
                "status":d["status"],"stage":d["stage"],"ready_for_main":d["ready_for_main"]}

    def task_matches_filters(card,*,status,repository,agent,sandbox_status,test_status,integration_status,q):
        t=card["task"]
        if status and card["column"]!=status: return False
        if repository and repository not in card["repos"]: return False
        if agent and not any(w["agent"]==agent for w in card["workspaces"]): return False
        if sandbox_status=="running" and card["sandbox"]["running"]==0: return False
        if sandbox_status=="unhealthy" and card["sandbox"]["unhealthy"]==0: return False
        if sandbox_status=="none" and card["sandbox"]["total"]>0: return False
        if test_status=="pass" and not (card["tests"]["total"] and card["tests"]["passed"]==card["tests"]["total"]): return False
        if test_status=="fail" and card["agents"]["failed"]==0: return False
        if test_status=="not_run" and card["tests"]["passed"]>0: return False
        if integration_status and card["integration"]!=integration_status: return False
        if q and q.lower() not in t["title"].lower() and q.lower() not in t["slug"].lower(): return False
        return True

    def parse_filters(status,repository,agent,sandbox_status,test_status,integration_status,q):
        f={"status":status,"repository":repository,"agent":agent,"sandbox_status":sandbox_status,"test_status":test_status,"integration_status":integration_status,"q":q}
        return f,urlencode({k:v for k,v in f.items() if v})

    @app.get("/tasks",response_class=HTMLResponse)
    def tasks_page(request:Request,status:str="",repository:str="",agent:str="",sandbox_status:str="",test_status:str="",integration_status:str="",q:str=""):
        filters,qs=parse_filters(status,repository,agent,sandbox_status,test_status,integration_status,q)
        rows=_filter_rows(db.all("SELECT * FROM tasks ORDER BY updated_at DESC"),_visible_task_ids(request))
        cards=[task_card_view(t) for t in rows]
        filtered=[c for c in cards if task_matches_filters(c,**filters)]
        return render(request,"tasks.html",cards=filtered,filters=filters,filters_qs=qs,columns=KANBAN_COLUMNS,
                      repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),agents=settings.agents)
    @app.get("/kanban",response_class=HTMLResponse)
    def kanban_page(request:Request,status:str="",repository:str="",agent:str="",sandbox_status:str="",test_status:str="",integration_status:str="",q:str=""):
        filters,qs=parse_filters(status,repository,agent,sandbox_status,test_status,integration_status,q)
        rows=_filter_rows(db.all("SELECT * FROM tasks WHERE status NOT IN ('CANCELLED') ORDER BY updated_at DESC"),_visible_task_ids(request))
        cards=[task_card_view(t) for t in rows]
        filtered=[c for c in cards if task_matches_filters(c,**filters)]
        board={col:[c for c in filtered if c["column"]==col] for col in KANBAN_COLUMNS}
        return render(request,"kanban.html",board=board,columns=KANBAN_COLUMNS,filters=filters,filters_qs=qs,
                      repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),agents=settings.agents)
    @app.post("/api/tasks")
    def create_task(request:Request,title:str=Form(...),description:str=Form(""),priority:str=Form("NORMAL"),tags:str=Form(""),repo_scope_id:str=Form(""),notes:str=Form(""),risk_profile:str=Form("NORMAL"), _csrf: None = Depends(_mutating_csrf)):
        """The primary Task creation flow (section 1): a Task lands in
        BACKLOG with no branch/worktree/sandbox allocated at all -- those
        only appear once the Task is explicitly Selected (see /select) and
        an Agent Workspace is explicitly created (see add_task_workspace's
        BACKLOG gate below)."""
        slug=slugify(title); priority=priority.strip().upper() or "NORMAL"; risk_profile=risk_profile.strip().upper()
        if risk_profile not in RISK_PROFILES: risk_profile="NORMAL"
        scope=int(repo_scope_id) if repo_scope_id.strip().isdigit() else None
        # B0.3: a blank repo_scope_id is a legitimate orgless BACKLOG task
        # (allowed through); a given one must resolve to an org this user
        # can act in.
        _require_org_role_for_repo(request, scope, "MEMBER")
        tid=db.execute("INSERT INTO tasks(slug,title,description,status,priority,tags,repo_scope_id,notes,risk_profile) VALUES(?,?,?,?,?,?,?,?,?)",
                        (slug,title,description,"BACKLOG",priority,tags.strip(),scope,notes.strip(),risk_profile))
        db.event("task",tid,"TASK_CREATED_BACKLOG",slug); return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/select")
    def select_task(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """[Select for Development] (section 7): BACKLOG -> ACTIVE. Still
        allocates nothing -- Task Stage stays PLANNING (computed) until an
        Agent Workspace actually exists."""
        t=task_row(tid)
        if t["status"]!="BACKLOG": raise GitSafetyError(f"Task is not in BACKLOG (status={t['status']})")
        db.execute("UPDATE tasks SET status='ACTIVE',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_SELECTED")
        return RedirectResponse(f"/tasks/{tid}",303)
    BRIEF_FIELDS=("brief_goal","brief_context","brief_requirements","brief_acceptance_criteria","brief_out_of_scope","brief_test_plan","brief_risks")
    @app.post("/api/tasks/{tid}/brief")
    def save_brief(tid:int,goal:str=Form(""),context:str=Form(""),requirements:str=Form(""),acceptance_criteria:str=Form(""),out_of_scope:str=Form(""),test_plan:str=Form(""),risks:str=Form(""),risk_profile:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Implementation Brief (section 8): structured fields, saved in
        place (one current brief per Task, not a history log) -- but
        brief_version bumps whenever the content that actually drives a
        Builder/Reviewer prompt changes, which is what makes existing
        Review/QA evidence recompute STALE (TaskDecisionService.
        builder_view) instead of silently staying valid."""
        t=task_row(tid)
        new_values={"brief_goal":goal.strip(),"brief_context":context.strip(),"brief_requirements":requirements.strip(),
                    "brief_acceptance_criteria":acceptance_criteria.strip(),"brief_out_of_scope":out_of_scope.strip(),
                    "brief_test_plan":test_plan.strip(),"brief_risks":risks.strip()}
        changed=any((t.get(f) or "")!=new_values[f] for f in BRIEF_FIELDS)
        rp=risk_profile.strip().upper()
        sets=[f"{f}=?" for f in BRIEF_FIELDS]+["updated_at=CURRENT_TIMESTAMP"]
        params=[new_values[f] for f in BRIEF_FIELDS]
        if rp in RISK_PROFILES: sets.insert(-1,"risk_profile=?"); params.append(rp)
        if changed: sets.insert(-1,"brief_version=brief_version+1")
        db.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?",(*params,tid))
        db.event("task",tid,"BRIEF_SAVED",f"brief_version+1={changed}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/generate-prompt")
    def generate_prompt(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Fill AGENT PROMPT from the Task's current prompt/brief
        (deterministic template, never a model call) -- the user still
        reviews/edits it before any agent is launched (section 3). Kept
        for the legacy structured-brief flow's own explicit button; the
        new prompt-first flow calls regenerate_agent_prompt() itself on
        create/edit instead of requiring this extra click."""
        ws=task_workspaces(tid)
        repo_row=repo(ws[0]["repository_id"]) if ws else None
        regenerate_agent_prompt(tid,repo_row)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/prompt")
    def save_prompt(tid:int,implementation_prompt:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Edit the Implementation Prompt (section: 'do not rewrite the
        user's prompt silently' -- only the user changes this text, ever).
        Bumps brief_version on an actual content change, the exact same
        mechanism save_brief already uses for the legacy structured
        fields, so TaskDecisionService.builder_view() flips any Review/QA
        pinned to the old version to STALE automatically -- no separate
        prompt_version bookkeeping needed. Regenerates the derived
        agent_prompt so it always reflects the latest saved intent."""
        t=task_row(tid)
        new_prompt=implementation_prompt.strip()
        changed=(t.get("implementation_prompt") or "")!=new_prompt
        sets=["implementation_prompt=?","updated_at=CURRENT_TIMESTAMP"]
        params=[new_prompt]
        if changed: sets.insert(-1,"brief_version=brief_version+1")
        db.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?",(*params,tid))
        db.event("task",tid,"PROMPT_SAVED",f"brief_version+1={changed}")
        if new_prompt:
            ws=task_workspaces(tid)
            repo_row=repo(ws[0]["repository_id"]) if ws else None
            regenerate_agent_prompt(tid,repo_row)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/agent-prompt")
    def save_agent_prompt(tid:int,agent_prompt:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        t=task_row(tid); db.execute("UPDATE tasks SET agent_prompt=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(agent_prompt,tid))
        latest=db.one("SELECT id FROM prompts WHERE task_id=? AND prompt_type='BUILDER' ORDER BY id DESC LIMIT 1",(tid,))
        if latest: db.execute("UPDATE prompts SET content=? WHERE id=?",(agent_prompt,latest["id"]))
        else: db.execute("INSERT INTO prompts(task_id,prompt_type,brief_version,content) VALUES(?,?,?,?)",(tid,"BUILDER",t["brief_version"],agent_prompt))
        return RedirectResponse(f"/tasks/{tid}",303)
    def latest_prompt(tid,prompt_type="BUILDER"):
        return db.one("SELECT * FROM prompts WHERE task_id=? AND prompt_type=? ORDER BY id DESC LIMIT 1",(tid,prompt_type))
    @app.post("/api/tasks/new-with-workspace")
    async def create_task_with_workspace(request:Request, _csrf: None = Depends(_mutating_csrf)):
        """Advanced/quick-start shortcut: Task + at least one Agent
        Workspace in one submit (optionally many, for a cross-repo Task
        defined immediately), skipping BACKLOG for when planning is
        unnecessary -- created directly ACTIVE since a Builder Workspace
        is being attached in the same request (section 5/7). A failed
        workspace never rolls back the Task or any already-created
        workspace -- it is recorded and shown, not hidden."""
        form=await request.form()
        title=str(form.get("title","")).strip()
        if not title: raise GitSafetyError("Task title is required")
        description=str(form.get("description",""))
        repo_ids=form.getlist("ws_repository_id"); agents=form.getlist("ws_agent"); roles=form.getlist("ws_role")
        bases=form.getlist("ws_base_branch"); profiles=form.getlist("ws_sandbox_profile")
        # B0.3: role required in every listed repository's org before any
        # Task/Workspace is actually created (fail before the INSERT, not
        # after -- a partially-created cross-repo Task is a worse failure
        # mode than refusing up front).
        _require_org_role_for_repos(
            request, [int(r) for r in repo_ids if str(r).strip().isdigit()], "MEMBER")
        slug=slugify(title); tid=db.execute("INSERT INTO tasks(slug,title,description,status) VALUES(?,?,?,?)",(slug,title,description,"ACTIVE"))
        db.event("task",tid,"TASK_CREATED",slug)
        for i,rid_raw in enumerate(repo_ids):
            if not str(rid_raw).strip(): continue
            agent=agents[i] if i<len(agents) else ""
            if not agent: continue
            role=roles[i] if i<len(roles) else ""; base=(bases[i] if i<len(bases) else "main") or "main"
            profile=profiles[i] if i<len(profiles) else ""
            result=add_task_workspace(tid,int(rid_raw),agent,role,base,profile)
            if not result["ok"]: db.event("task",tid,"WORKSPACE_CREATE_FAILED",f"{agent} · repo #{rid_raw}: {result['error']}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/create")
    async def create_task_unified(request:Request, _csrf: None = Depends(_mutating_csrf)):
        """The primary, simplified Task Create flow: ONE Implementation
        Prompt instead of a structured Brief form (Task title / Prompt /
        Repository / Agent / Sandbox, everything else tucked under
        Advanced). Repository+Agent are optional: filled in, the Task goes
        straight to ACTIVE with its first Builder Workspace ("Create &
        Start"); left blank, it lands in BACKLOG with just the prompt
        saved as intent -- the same BACKLOG contract as /api/tasks (no
        branch/worktree/sandbox allocated at all). Advanced's "additional
        repositories" reuses the exact ws_repository_id/ws_agent/ws_role/
        ws_base_branch/ws_sandbox_profile array fields /new-with-workspace
        already established, so a cross-repo Task can still be defined in
        one submit. A failed workspace never rolls back the Task or any
        already-created workspace, same as /new-with-workspace."""
        form=await request.form()
        title=str(form.get("title","")).strip()
        if not title: raise GitSafetyError("Task title is required")
        prompt=str(form.get("implementation_prompt","")).strip()
        risk_profile=str(form.get("risk_profile","NORMAL")).strip().upper()
        if risk_profile not in RISK_PROFILES: risk_profile="NORMAL"

        rows=[]
        primary_repo=str(form.get("repository_id","")).strip()
        primary_agent=str(form.get("agent","")).strip()
        if primary_repo and primary_agent:
            rows.append((primary_repo,primary_agent,str(form.get("primary_role","")).strip(),
                         str(form.get("primary_base_branch","main")).strip() or "main",
                         str(form.get("sandbox_profile","")).strip()))
        extra_repo=form.getlist("ws_repository_id"); extra_agent=form.getlist("ws_agent")
        extra_role=form.getlist("ws_role"); extra_base=form.getlist("ws_base_branch"); extra_profile=form.getlist("ws_sandbox_profile")
        for i,rid_raw in enumerate(extra_repo):
            if not str(rid_raw).strip(): continue
            a=extra_agent[i] if i<len(extra_agent) else ""
            if not a: continue
            rows.append((rid_raw,a,extra_role[i] if i<len(extra_role) else "",
                         (extra_base[i] if i<len(extra_base) else "main") or "main",
                         extra_profile[i] if i<len(extra_profile) else ""))

        # B0.3: same rule as /api/tasks/new-with-workspace -- role required
        # in every listed repository's org before creating anything.
        _require_org_role_for_repos(
            request, [int(r[0]) for r in rows if str(r[0]).strip().isdigit()], "MEMBER")
        slug=slugify(title); status="ACTIVE" if rows else "BACKLOG"
        tid=db.execute("INSERT INTO tasks(slug,title,status,risk_profile,implementation_prompt) VALUES(?,?,?,?,?)",
                        (slug,title,status,risk_profile,prompt))
        db.event("task",tid,"TASK_CREATED",slug)

        primary_repo_row=None
        for rid_raw,agent,role,base,profile in rows:
            try: rid=int(rid_raw)
            except ValueError: db.event("task",tid,"WORKSPACE_CREATE_FAILED",f"{agent} · invalid repository id {rid_raw!r}"); continue
            result=add_task_workspace(tid,rid,agent,role,base,profile)
            if not result["ok"]: db.event("task",tid,"WORKSPACE_CREATE_FAILED",f"{agent} · repo #{rid_raw}: {result['error']}")
            elif primary_repo_row is None:
                try: primary_repo_row=repo(rid)
                except HTTPException: pass

        if prompt: regenerate_agent_prompt(tid,primary_repo_row)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.get("/api/tasks")
    def api_tasks(request: Request): return _filter_rows(db.all("SELECT * FROM tasks ORDER BY updated_at DESC"),_visible_task_ids(request))
    @app.get("/tasks/{tid}",response_class=HTMLResponse)
    def task_detail(request:Request,tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        """Every status/stage/gate/next-action value on this page comes
        from one call to decision.evaluate() (TaskDecisionService) --
        this route only attaches display-only detail (git worktree info,
        live session, per-workspace/integration sandbox view) onto that
        result. No independent readiness/stage computation lives here
        any more (section 32/37)."""
        t=task_row(tid)
        d=decision.evaluate(tid)
        workspaces=d["builders"]; ti=d["task_integration"]; ti_repos=d["integration_repos"]
        sbxs=task_sandboxes(tid)

        for w in workspaces:
            w["details"]=safe_details(w["worktree_path"]); w["status_label"]=workspace_status_label(w["status"])
            # E8.5.25: managed-worktree lifecycle/staleness -- computed,
            # cheap (no probe merge; integration-check is its own
            # on-demand action, never run on every page view).
            w["worktree_lifecycle_status"]=worktree_manager.lifecycle_status(w,tid)
            w["worktree_staleness"]=worktree_manager.check_staleness(tid,ws=w)
            session=latest_session_for_workspace(w["id"]); w["session"]=session
            # Section 1/3 of the Live Terminal fix: a session only has an
            # actual terminal to attach to while it's STARTING/RUNNING/
            # WAITING_FOR_INPUT -- the latest session for a workspace is
            # very often already EXITED/FAILED (the normal end state after
            # a Builder finishes and submits for review), and a template
            # must never render "Open Live Terminal" pointing at a dead
            # session's route as if it were still attachable.
            w["live_session"]=session if session and session["status"] in LIVE_SESSION_STATUSES else None
            # Builder-completion-vs-live-session fix (section 3/4): the
            # agent's own completion report is only ever PRINTED to its
            # terminal -- there is no API callback wired up for it to
            # call. Detect a well-formed report in the session's own live
            # transcript (never auto-submitted -- section 4/13) so a
            # human gets one clear, one-click confirm action instead of
            # having to notice, copy and manually retype it into the
            # Edit Agent Report form themselves.
            w["detected_report"]=None
            if session and not w["ready"]:
                w["detected_report"]=parse_completion_report(agent_sessions.live_tail(session["id"]))
            # EXITED-without-report dead-end fix: AgentSession EXITED never
            # by itself means the Builder failed (section 8) -- it can just
            # as easily mean the source work finished but the process
            # exited before/without a report ever getting persisted. Only
            # compute the real-worktree validation (never a guess) when
            # there is genuinely no other evidence of completion yet.
            w["recovery_state"]="COMPLETION_REQUIRED" if w["agent_status"] in ("EXITED","FAILED") and not w["ready"] and not w["detected_report"] else None
            w["manual_ready_check"]=validate_manual_ready(w) if w["recovery_state"] else None
            w_repo=repo(w["repository_id"])
            try: w["sandbox_configured"]=load_sandbox_contract(Path(w_repo["repo_path"])) is not None
            except SandboxContractError: w["sandbox_configured"]=True  # misconfigured contract is not "absent"
            # Live per-Builder Workspace prompt (Task/Title-fallback + role
            # + Builder Instructions + sandbox + AGENTS.md) -- always
            # freshly computed for display, never a stored/stale copy.
            w["live_prompt"]=workspace_agent_prompt(w,t,w_repo)
            # Real incident (Task #6): sbxs is ORDER BY id ASC -- a plain
            # next() here picked this workspace's OLDEST sandbox (already
            # CLOSED) instead of its current one, so the wizard's Runtime
            # Verification panel showed a dead port while
            # TaskDecisionService (which already correctly orders DESC)
            # showed the real, RUNNING one. Two places computing "this
            # workspace's sandbox" must never disagree -- search latest
            # first here too.
            sb=next((s for s in reversed(sbxs) if s["owner_type"]=="AGENT_WORKSPACE" and s["owner_id"]==w["id"]),None)
            if sb:
                v=sandbox_view(sb); v["stale"]=sandboxes.is_stale(sb["id"],sandbox_current_commits(sb["id"]))
                w["sandbox"]=v
            else:
                w["sandbox"]=None

        integration_sandboxes=[]
        for sb in sbxs:
            if sb["owner_type"]!="TASK_INTEGRATION": continue
            v=sandbox_view(sb); v["stale"]=sandboxes.is_stale(sb["id"],sandbox_current_commits(sb["id"]))
            v["sources"]=db.all("SELECT s.*,r.repo_name FROM sandbox_sources s JOIN repositories r ON r.id=s.repository_id WHERE s.sandbox_id=?",(sb["id"],))
            v["hardware"]=db.one("SELECT * FROM hardware_test_results WHERE sandbox_id=? ORDER BY id DESC LIMIT 1",(sb["id"],))
            integration_sandboxes.append(v)

        earliest_cleanup=min((sb["cleanup_eligible_at"] for sb in sbxs if sb["cleanup_eligible_at"]),default=None)
        blocking_workspace=next((w for w in workspaces if w["fix_required"]), next((w for w in workspaces if not w["ready"]),None))
        not_ready=[w for w in workspaces if not w["ready"]]

        # Real merge tracking (section 4/9): every MergeRecord's live
        # blocker list, computed fresh from the same `d` the rest of the
        # page already reads -- never a second, template-side gate.
        for m in d["merge_records"]:
            try: m_repo=repo(m["repository_id"])
            except HTTPException: m_repo=None
            m["github_available"]=bool(m_repo) and github_merge.available(m_repo["repo_path"])
            m["gate"]=decision.merge_gate_status(d,m["repository_id"],m)
            repo_ti=next((x for x in ti_repos if x["repository_id"]==m["repository_id"]),None)
            m["integration_id"]=repo_ti["id"] if repo_ti else None
            # Button-state-ux: Create PR / Merge PR feedback, keyed by
            # this exact MergeRecord row (never repository_id alone --
            # the same repo can have a MergeRecord per Task).
            m["create_pr_op"]=ops.latest("merge_record",m["id"],"CREATE_PR")
            m["merge_pr_op"]=ops.latest("merge_record",m["id"],"MERGE_PR")
            # Deployment (section 6/23): only ever meaningful once this
            # repo is actually MERGED -- computed for every repo anyway
            # (cheap, no real command execution) so a repo that merges
            # AFTER Task DONE also gets its own Deployment section
            # without a second code path.
            if m["merge_status"]=="MERGED":
                dep_target=deployer.target(m_repo["repo_path"],"DEV") if m_repo else None
                dep_row=latest_deployment(tid,m["repository_id"],"DEV")
                rb_target=deployer.rollback_target(dep_row) if dep_row else None
                m["deployment_dev"]=deployment_view(dep_row,bool(dep_target),rb_target)
            else:
                m["deployment_dev"]=None

        # Overview gate checklist (section 38): one row per gate this
        # Task's risk policy actually requires, ✓/○ from the same
        # decision the rest of the page reads -- explains exactly why
        # not ready, never a second ad hoc readiness calculation.
        gates=[{"label":"Task intent resolvable","ok":decision.brief_complete(t),"note":f"source: {d['prompt_source'].replace('_',' ').title()}"},
               {"label":"At least one Builder Workspace","ok":bool(workspaces)},
               {"label":"All Builder Workspaces submitted for review","ok":bool(workspaces) and all(b["ready"] for b in workspaces)},
               {"label":"All reviews PASS (exact commit, current Brief)","ok":bool(workspaces) and all(b["review_status"]=="PASS" for b in workspaces)}]
        if decision.requires_qa(d["risk_profile"]):
            gates.append({"label":"QA PASS (current Brief)","ok":decision.qa_current(d["qa"],t) and d["qa"]["status"]=="PASS"})
        else:
            gates.append({"label":"QA","ok":True,"note":"NOT_REQUIRED for this risk profile"})
        if decision.requires_integration(d["risk_profile"]):
            # Section 16: READY_FOR_MAIN must never look like a plain PASS
            # when it only got there via an approved baseline waiver --
            # count waived failures across every participating repo's
            # live gate_status so the checklist says PASS WITH N BASELINE
            # WAIVER, never a silent, indistinguishable-from-real PASS.
            waived_count=sum(1 for r in ti_repos for f in ((r.get("gate_status") or {}).get("failures") or []) if f["classification"]=="WAIVED")
            all_merged_gate=bool(d["merge_records"]) and all(m["merge_status"]=="MERGED" for m in d["merge_records"] if m["required"])
            note=f"PASS WITH {waived_count} BASELINE WAIVER" if waived_count and decision.integration_healthy(ti,ti_repos,all_merged_gate) else None
            gates.append({"label":"Integration healthy, tests PASS, no conflicts","ok":decision.integration_healthy(ti,ti_repos,all_merged_gate),"note":note})
        else:
            gates.append({"label":"Integration","ok":True,"note":"NOT_REQUIRED for this risk profile"})
        gates.append({"label":"No blocking findings","ok":not d["blocking_reasons"]})
        gates.append({"label":"All required repos merged to main","ok":bool(d["merge_records"]) and all(m["merge_status"]=="MERGED" for m in d["merge_records"] if m["required"])})

        # Default reviewer differs from the builder's own agent when
        # another trusted agent is configured (section 10) -- computed
        # once per workspace, purely a UI default, never enforced.
        for w in workspaces:
            other=[a for a in settings.agents if a!=w["agent"]]
            w["default_reviewer"]=other[0] if other else w["agent"]
            w["last_activity_hint"]=activity_summary(w["session"]["id"], 200) if w["session"] else None

        qa_required=decision.requires_qa(d["risk_profile"]); integration_required=decision.requires_integration(d["risk_profile"])
        # E9.32: Review/Fix panel -- read-only, all from ReviewFixOrchestratorService's
        # own status()/integration_readiness() (never a second computation here).
        review_fix=review_fix_orchestrator.status(tid)
        review_fix["findings"]=findings_store.list_for_task(task_chain_ids(db,tid))
        try: review_fix["integration_readiness"]=review_fix_orchestrator.integration_readiness(tid)
        except Exception: review_fix["integration_readiness"]=None
        return render(request,"task_detail.html",t=t,decision=d,workspaces=workspaces,sandboxes=sbxs,task_integration=ti,ti_repos=ti_repos,
                      review_fix=review_fix,
                      integration_sandboxes=integration_sandboxes,
                      status=d["status"],stage=d["stage"],risk_profile=d["risk_profile"],next_action=d["next_action"],
                      blocking_reasons=d["blocking_reasons"],test_readiness=d["test_readiness"],ready_for_main=d["ready_for_main"],
                      merge_records=d["merge_records"],qa=d["qa"],gates=gates,current_step=d["current_step"],
                      prompt_source=d["prompt_source"],effective_task_prompt=d["effective_task_prompt"],
                      qa_required=qa_required,
                      task_cleanup_countdown=format_countdown(earliest_cleanup),
                      blocking_workspace=blocking_workspace,not_ready_workspaces=not_ready,
                      repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),agents=settings.agents,
                      user_state=user_task_state(d),progress=progress_summary(d["current_step"],qa_required,integration_required))
    @app.get("/api/tasks/{tid}")
    def api_task(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        t=task_row(tid); return {**t,"workspaces":task_workspaces(tid),"sandboxes":task_sandboxes(tid),"task_integration":task_integration_row(tid)}
    @app.get("/api/tasks/{tid}/decision")
    def api_task_decision(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        """The exact TaskDecisionService.evaluate() result -- the single
        source every page on this Task reads (section 32). Exposed
        directly so automation/tests never have to re-derive status/
        stage/gates from raw child rows themselves."""
        task_row(tid); return decision.evaluate(tid)

    # ---------------------------------------------------------- Spec Layer
    @app.get("/api/spec/registry")
    def api_spec_registry():
        """Spec Layer status (S3): load errors (if any), the deterministic
        baseline digest, and feature/requirement/acceptance/invariant
        counts -- the one place to check "is the canonical spec tree
        currently valid" without walking specs/ by hand."""
        try:
            registry=SpecRegistry(specs_root).load()
        except SpecError as exc:
            return JSONResponse({"ok":False,"errors":exc.errors},status_code=422)
        return {"ok":True,"baseline_sha256":registry.baseline_digest(),
                "features":len(registry.features),"requirements":len(registry.requirements),
                "acceptance_criteria":len(registry.acceptance),"invariants":len(registry.invariants)}
    @app.get("/api/spec/features")
    def api_spec_features():
        registry=SpecRegistry(specs_root).load()
        return [{"id":f["id"],"title":f.get("title"),"version":f.get("version"),"status":f.get("status")} for f in registry.features.values()]
    @app.get("/api/spec/features/{feature_id}")
    def api_spec_feature(feature_id:str):
        registry=SpecRegistry(specs_root).load()
        feature=registry.feature(feature_id)
        if not feature: raise HTTPException(404,"Unknown feature id")
        return {k:v for k,v in feature.items() if k!="_path"}
    @app.post("/api/tasks/{tid}/spec")
    def save_task_spec(tid:int,classification:str=Form(""),feature_id:str=Form(""),spec_version:str=Form(""),
                        requirement_ids:str=Form(""),acceptance_ids:str=Form(""),invariant_ids:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Task/spec TraceLink (S4): sets a Task's spec_* linkage.
        Deliberately permissive at write time (this route never itself
        validates against the registry -- SpecGate does that, every
        time, right before an Agent would start) so linking a Task to a
        not-yet-approved or in-progress spec is never blocked; only
        STARTING an Agent for a behavior-changing Task is."""
        task_row(tid)
        cls=classification.strip().upper() or None
        if cls and cls not in ALL_CLASSIFICATIONS:
            raise GitSafetyError(f"Unknown change classification: {cls} (must be one of {ALL_CLASSIFICATIONS})")
        def _ids(raw):
            return json.dumps([x.strip() for x in raw.replace(",","\n").splitlines() if x.strip()])
        db.execute(
            "UPDATE tasks SET spec_change_classification=?,spec_feature_id=?,spec_version=?,spec_requirement_ids=?,spec_acceptance_ids=?,spec_invariant_ids=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (cls,feature_id.strip() or None,int(spec_version) if spec_version.strip().isdigit() else None,
             _ids(requirement_ids),_ids(acceptance_ids),_ids(invariant_ids),tid))
        db.event("task",tid,"SPEC_LINKAGE_UPDATED",f"classification={cls} feature_id={feature_id.strip() or None}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.get("/api/tasks/{tid}/spec-gate")
    def api_task_spec_gate(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        t=task_row(tid); return spec_gate.evaluate(t)
    @app.get("/api/tasks/{tid}/spec-compliance")
    def api_task_spec_compliance(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid); return spec_compliance.verify(tid)
    @app.get("/api/tasks/{tid}/evidence")
    def api_task_evidence(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid); return evidence_store.for_task(tid)

    # ---------------------------------------------- Engineering Domain (E1)
    # Change -> Task, Change/Task -> WorkProduct, Task -> input/output
    # WorkProduct. Additive layer above the existing Task model -- see
    # app/services/change_service.py / work_product_service.py for the
    # domain logic itself; these routes are thin HTTP adapters over it,
    # the same shape every other API route in this file already has.
    def change_row(cid:int):
        row=changes.get(cid)
        if not row: raise HTTPException(404,"Change not found")
        return row
    def work_product_row(wpid:int):
        row=work_products.get(wpid)
        if not row: raise HTTPException(404,"WorkProduct not found")
        return row

    # ------------------------------------------------------ Change Overview (UI)
    # The first UI surface for the whole E1-E7 engineering domain -- every
    # phase through E7 was deliberately API-only. Nothing here computes a
    # new status: build_change_overview (app/services/change_overview.py)
    # only arranges what WorkflowService/ArchitectureDesignLifecycleService/
    # TestDesignLifecycleService/SpecLifecycleService/HumanDecisionService
    # already decided, the same "view-model over an existing decision,
    # never a second one" discipline user_state_view.py established for
    # Task (see task_detail.html's own status-hero/wf-checklist).
    @app.get("/changes",response_class=HTMLResponse)
    def changes_page(request:Request,status:str="",change_type:str="",profile:str="",page:int=1,page_size:int=25):
        """Project Overview (E7.5.1) + Change List (E7.5.2) combined --
        Active Changes / Human Attention / filters, all derived from
        WorkflowService/HumanDecisionService's own real state, never a
        second status calculation.

        Track A1 (A1.2/A1.3/A1.5/A1.6/A1.8) perf fix: composition moved
        into ChangeListSummaryService, which (a) only computes the
        expensive WorkflowService.evaluate_workflow() per row for rows
        actually about to be SHOWN with it (the current page / Human
        Attention / Recent Activity -- see that service's own docstring),
        never for the full Change set on every request once it grows
        past one page, and (b) wraps the remaining composition in one
        db.memoize() scope so whatever IS computed doesn't re-issue
        identical reads. Confirmed live by scripts/benchmark_changes_list.py:
        ~140 TaskDecisionService.evaluate() calls and 1400+ DB connections
        for just 100 Changes before this fix. Read-only route: safe to
        memoize end-to-end, never wrap a route that writes in this scope."""
        repo_ids=_visible_repo_ids(request)
        summary=change_list_summary_service.build(
            status=status,change_type=change_type,profile=profile,page=page,page_size=page_size,visible_repo_ids=repo_ids)
        filter_qs=urlencode({k:v for k,v in {"status":status,"change_type":change_type,"profile":profile}.items() if v})
        return render(request,"changes.html",changes=summary["rows"],all_changes=summary["all_changes"],
                      human_attention=summary["human_attention"],recent=summary["recent"],
                      filters={"status":status,"change_type":change_type,"profile":profile},filter_qs=filter_qs,
                      change_types=CHANGE_TYPES,profiles=list(PROFILES),
                      page=summary["page"],page_size=summary["page_size"],
                      total=summary["total"],total_pages=summary["total_pages"],
                      repos=_filter_rows(db.all("SELECT id,repo_name FROM repositories WHERE enabled=1 ORDER BY repo_name"),repo_ids))
    @app.get("/changes/{cid}",response_class=HTMLResponse)
    def change_detail(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        # Track A1.11-A1.13: Simple Mode replaces this hub page's
        # rendering only -- every Advanced tab route below (spec/
        # architecture/design/tests/plan/tasks/reviews/decisions/
        # evidence/release/deploy/acceptance) is completely untouched
        # and stays one click away (A1.12/A1.27), reached via this same
        # page's Advanced link or directly by URL.
        if _ui_mode(request)=="simple":
            return render(request,"change_detail_simple.html",cid=cid,
                          view=simple_view_service.build(cid))
        header=change_control_surface.header(cid)
        overview=change_control_surface.overview(cid)
        # E8.23: AUTONOMOUS EXECUTION card. Composed here at the route,
        # not inside ChangeControlSurfaceService, since autonomous_execution_service
        # is constructed later in create_app() (it needs add_task_workspace/
        # _start_builder_session) -- both names resolve fine at request
        # time regardless of definition order within this same closure.
        overview["autonomous_execution"]=autonomous_execution_service.status(cid)
        # E13.38: PARALLEL section on the same card -- composed here for
        # the same construction-order reason as above (execution_wave_service
        # needs release_service/integration_service, built later).
        change_row_data=change_row(cid)
        parallel_policy=execution_wave_service.get_parallel_policy(change_row_data)
        current_wave=execution_wave_service.current_wave_for_change(cid)
        overview["parallel_execution"]={
            "enabled":parallel_policy["enabled"],
            "active_builders":autonomous_execution_service._live_builder_count(cid),
            "max_concurrent_builders":autonomous_execution_service.get_policy(change_row_data)["max_concurrent_builders"],
            "current_wave":current_wave,
        }
        return render(request,"change_detail.html",cid=cid,active_tab="",header=header,overview=overview)
    @app.get("/changes/{cid}/spec",response_class=HTMLResponse)
    def change_spec_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_spec.html",cid=cid,active_tab="spec",header=change_control_surface.header(cid),
                      data=change_control_surface.spec_tab(cid))
    @app.get("/changes/{cid}/architecture",response_class=HTMLResponse)
    def change_architecture_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_architecture.html",cid=cid,active_tab="architecture",header=change_control_surface.header(cid),
                      data=change_control_surface.architecture_tab(cid))
    @app.get("/changes/{cid}/design",response_class=HTMLResponse)
    def change_design_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_design.html",cid=cid,active_tab="design",header=change_control_surface.header(cid),
                      data=change_control_surface.design_tab(cid))
    @app.get("/changes/{cid}/tests",response_class=HTMLResponse)
    def change_tests_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_tests.html",cid=cid,active_tab="tests",header=change_control_surface.header(cid),
                      data=change_control_surface.tests_tab(cid))
    @app.get("/changes/{cid}/plan",response_class=HTMLResponse)
    def change_plan_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_plan.html",cid=cid,active_tab="plan",header=change_control_surface.header(cid),
                      data=change_control_surface.plan_tab(cid))
    @app.get("/changes/{cid}/tasks",response_class=HTMLResponse)
    def change_tasks_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        data=change_control_surface.tasks_tab(cid)
        # E8.23: per-row execution readiness (AUTO_READY/WAITING_.../
        # STALE_PLAN/...) -- read-only annotation, same evaluate_task()
        # the API/scheduler itself uses, never a second readiness engine.
        for row in data["rows"]:
            row["execution_readiness"]=autonomous_execution_service.evaluate_task(row["task"]["id"])
            # E13.38: per-row Parallel Safety/Wave/Active Session --
            # read-only annotation over the same execution_wave_tasks
            # audit rows/ParallelSafetyService the API itself uses,
            # never a second scheduler truth.
            tid=row["task"]["id"]
            ewt=db.one("SELECT ewt.*,ew.wave_number FROM execution_wave_tasks ewt JOIN execution_waves ew ON ew.id=ewt.wave_id "
                       "WHERE ewt.task_id=? ORDER BY ewt.id DESC LIMIT 1",(tid,))
            row["wave"]=ewt
            live=db.one("SELECT * FROM agent_sessions WHERE task_id=? ORDER BY id DESC LIMIT 1",(tid,))
            row["active_session"]=live if live and live["status"] in ("STARTING","RUNNING","WAITING_FOR_INPUT") else None
        return render(request,"change_tasks.html",cid=cid,active_tab="tasks",header=change_control_surface.header(cid),
                      data=data)
    @app.get("/changes/{cid}/reviews",response_class=HTMLResponse)
    def change_reviews_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_reviews.html",cid=cid,active_tab="reviews",header=change_control_surface.header(cid),
                      data=change_control_surface.reviews_tab(cid))
    @app.get("/changes/{cid}/decisions",response_class=HTMLResponse)
    def change_decisions_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_decisions.html",cid=cid,active_tab="decisions",header=change_control_surface.header(cid),
                      data=change_control_surface.decisions_tab(cid))
    @app.get("/changes/{cid}/evidence",response_class=HTMLResponse)
    def change_evidence_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_evidence.html",cid=cid,active_tab="evidence",header=change_control_surface.header(cid),
                      data=change_control_surface.evidence_tab(cid))
    @app.get("/changes/{cid}/release",response_class=HTMLResponse)
    def change_release_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_release.html",cid=cid,active_tab="release",header=change_control_surface.header(cid),
                      data=change_control_surface.release_tab(cid))
    @app.get("/changes/{cid}/deploy",response_class=HTMLResponse)
    def change_deploy_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_deploy.html",cid=cid,active_tab="deploy",header=change_control_surface.header(cid),
                      data=change_control_surface.deploy_tab(cid))
    @app.get("/changes/{cid}/acceptance",response_class=HTMLResponse)
    def change_acceptance_tab(request:Request,cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return render(request,"change_acceptance.html",cid=cid,active_tab="acceptance",header=change_control_surface.header(cid),
                      data=change_control_surface.acceptance_tab(cid))

    # ------------------------------------------------------ Incidents (E12 UI)
    @app.get("/incidents",response_class=HTMLResponse)
    def incidents_page(request:Request,status:str="",project_id:str=""):
        pid=int(project_id) if project_id.strip().isdigit() else None
        rows=_filter_polymorphic(request,"incident",incident_service.list(project_id=pid,status=status.strip() or None))
        for r in rows:
            r["change"]=changes.get(r["change_id"]) if r["change_id"] else None
        open_rows=[r for r in rows if r["status"] not in ("CLOSED",)]
        return render(request,"incidents.html",incidents=rows,open_incidents=open_rows,
                      filters={"status":status,"project_id":project_id},statuses=INCIDENT_STATUSES,
                      repos=db.all("SELECT * FROM repositories WHERE enabled=1"))
    @app.get("/incidents/{iid}",response_class=HTMLResponse)
    def incident_detail(request:Request,iid:int, _authz: None = Depends(require_read_role("incident", "iid"))):
        row=incident_row(iid)
        change=changes.get(row["change_id"]) if row["change_id"] else None
        workflow_state=workflow_service.evaluate_workflow(row["change_id"]) if row["change_id"] else None
        release=incident_service._current_release(row["change_id"]) if row["change_id"] else None
        governing_features=[l["target_id"] for l in trace.for_source("change",row["change_id"])
                              if l["target_type"]=="spec_feature"] if row["change_id"] else []
        return render(request,"incident_detail.html",incident=row,change=change,workflow_state=workflow_state,
                      release=release,governing_features=governing_features,
                      regression_history=incident_service.regression_history(iid),
                      classifications=INCIDENT_CLASSIFICATIONS,sources=INCIDENT_SOURCES,severities=INCIDENT_SEVERITIES,
                      test_case_specs=test_case_specs_store.list_for_change(row["change_id"]) if row["change_id"] else [])

    @app.get("/api/changes/{cid}/control-surface")
    def api_change_control_surface(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        """E7.5.19: one composed, read-only view across every existing
        E1-E7 service for this Change -- composition only, never a new
        business-state calculation. The HTML tab routes above call the
        same ChangeControlSurfaceService methods directly (no internal
        HTTP round-trip); this route exists for external/programmatic
        callers that want the whole surface in one request."""
        change_row(cid)
        return {
            "header": change_control_surface.header(cid), "overview": change_control_surface.overview(cid),
            "spec": change_control_surface.spec_tab(cid), "architecture": change_control_surface.architecture_tab(cid),
            "design": change_control_surface.design_tab(cid), "tests": change_control_surface.tests_tab(cid),
            "plan": change_control_surface.plan_tab(cid), "tasks": change_control_surface.tasks_tab(cid),
            "reviews": change_control_surface.reviews_tab(cid), "decisions": change_control_surface.decisions_tab(cid),
            "evidence": change_control_surface.evidence_tab(cid), "release": change_control_surface.release_tab(cid),
            "deploy": change_control_surface.deploy_tab(cid), "acceptance": change_control_surface.acceptance_tab(cid),
        }

    @app.get("/api/changes/{cid}/simple-view")
    def api_change_simple_view(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        """A1.23: the one Simple Mode composition endpoint -- same
        no-duplicate-truth discipline as control-surface above, built on
        top of it (SimpleViewService only ever reads ChangeControlSurfaceService's
        already-composed output). No duplicate persisted state anywhere."""
        change_row(cid)
        return simple_view_service.build(cid)

    @app.post("/changes/{cid}/human-decisions/{did}/resolve")
    def change_resolve_human_decision(cid:int,did:int,resolution_note:str=Form(...), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        """Form-friendly counterpart to the JSON /api/human-decisions/{did}/
        resolve route (E5.11) -- same HumanDecisionService.resolve() call,
        just a redirect back to this Change's Decisions tab (E7.5.13)
        instead of a JSON body, so the page's inline Resolve form is a
        real, working action rather than a read-only status mockup."""
        change_row(cid); human_decisions.resolve(did,resolution_note)
        return RedirectResponse(f"/changes/{cid}/decisions#pending",303)

    @app.get("/api/changes")
    def api_list_changes(request:Request,project_id:int|None=None):
        return _filter_rows(changes.list(project_id=project_id),_visible_repo_ids(request),"project_id")
    @app.post("/api/changes")
    def api_create_change(request:Request,title:str=Form(...),description:str=Form(""),change_type:str=Form("FEATURE"),
                           risk_level:str=Form("NORMAL"),project_id:str=Form(""), _csrf: None = Depends(_mutating_csrf)):
        pid=int(project_id) if project_id.strip().isdigit() else None
        _require_org_role_for_repo(request, pid, "MEMBER")
        cid=changes.create(title=title,description=description,change_type=change_type,risk_level=risk_level,project_id=pid)
        return change_row(cid)
    @app.post("/changes")
    def create_change_simple(request:Request,what:str=Form(...),change_type:str=Form("FEATURE"),project_id:str=Form(""), _csrf: None = Depends(_mutating_csrf)):
        """A1.19: the Simple Create Change entry -- one big "what do you
        want to change or build?" box, no Spec/Plan/Task vocabulary
        required. Reuses ChangeService.create() exactly (never a second
        creation path/second source of truth); the freeform text becomes
        BOTH description (kept in full) and title (first line, truncated
        to a sane length -- same "intent is always resolvable" fallback
        discipline task_decision_service.py's effective_task_prompt()
        already established for Task Title). Advanced fields (risk_level,
        project_id, an exact separate title) remain available via the
        existing POST /api/changes JSON route -- this is additive, not a
        replacement."""
        what=(what or "").strip()
        if not what:
            raise HTTPException(422,"Tell us what you want to change or build.")
        title=what.splitlines()[0].strip()[:120] or what[:120]
        pid=int(project_id) if project_id.strip().isdigit() else None
        _require_org_role_for_repo(request, pid, "MEMBER")
        cid=changes.create(title=title,description=what,change_type=change_type,project_id=pid)
        return RedirectResponse(f"/changes/{cid}",303)
    @app.get("/api/changes/{cid}")
    def api_get_change(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        return change_row(cid)
    @app.post("/api/changes/{cid}/lifecycle")
    def api_change_lifecycle(cid:int,state:str=Form(...), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        """Human-driven only (E1.1): no Supervisor/Agent code path in this
        phase ever calls ChangeService.set_lifecycle_state -- an Agent
        cannot arbitrarily mark a Change DONE."""
        changes.set_lifecycle_state(cid,state)
        return change_row(cid)
    @app.get("/api/changes/{cid}/tasks")
    def api_change_tasks(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid); return changes.list_tasks_for_change(cid)
    @app.post("/api/changes/{cid}/tasks/{tid}/attach")
    def api_attach_task_to_change(cid:int,tid:int, _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        changes.attach_task_to_change(cid,tid); return task_row(tid)
    @app.get("/api/changes/{cid}/work-products")
    def api_change_work_products(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid); return work_products.list_for_change(cid)

    @app.post("/api/work-products")
    def api_create_work_product(request:Request,kind:str=Form(...),title:str=Form(...),project_id:str=Form(""),
                                 change_id:str=Form(""),task_id:str=Form(""),status:str=Form("DRAFT"),
                                 content_ref:str=Form(""),content_metadata:str=Form(""),
                                 content_digest:str=Form(""),supersedes_id:str=Form(""), _csrf: None = Depends(_mutating_csrf)):
        try: metadata=json.loads(content_metadata) if content_metadata.strip() else {}
        except (TypeError, ValueError): raise WorkProductError("content_metadata must be valid JSON")
        # B0.3: same project_id -> change_id -> task_id fallback chain
        # AuthzService's own work_product resolver uses for an EXISTING
        # work product -- checked here against whichever reference is
        # actually given before create (all three blank is a legitimate
        # orgless work product, same as the underlying schema allows).
        _wp_pid=int(project_id) if project_id.strip().isdigit() else None
        _wp_cid=int(change_id) if change_id.strip().isdigit() else None
        _wp_tid=int(task_id) if task_id.strip().isdigit() else None
        if _wp_pid is not None: _require_org_role_for_repo(request, _wp_pid, "MEMBER")
        elif _wp_cid is not None: _require_org_role_for_change(request, _wp_cid, "MEMBER")
        elif _wp_tid is not None: _require_org_role_for_entity(request, "task", _wp_tid, "MEMBER")
        else: _require_login_only(request)
        wpid=work_products.create(
            kind=kind,title=title,
            project_id=int(project_id) if project_id.strip().isdigit() else None,
            change_id=int(change_id) if change_id.strip().isdigit() else None,
            task_id=int(task_id) if task_id.strip().isdigit() else None,
            status=status,content_ref=content_ref.strip() or None,content_metadata=metadata,
            content_digest=content_digest.strip() or None,
            supersedes_id=int(supersedes_id) if supersedes_id.strip().isdigit() else None)
        return work_product_row(wpid)
    @app.get("/api/work-products/{wpid}")
    def api_get_work_product(wpid:int, _authz: None = Depends(require_read_role("work_product", "wpid"))):
        return work_product_row(wpid)

    @app.get("/api/tasks/{tid}/work-products")
    def api_task_work_products(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return {"inputs":work_products.inputs_for_task(tid),"outputs":work_products.outputs_for_task(tid)}
    @app.post("/api/tasks/{tid}/work-products/{wpid}/link")
    def api_link_task_work_product(tid:int,wpid:int,direction:str=Form(...), _authz: None = Depends(require_role("work_product", "wpid", "MEMBER"))):
        work_products.link_task(tid,wpid,direction)
        return {"inputs":work_products.inputs_for_task(tid),"outputs":work_products.outputs_for_task(tid)}

    # ---------------------------------- Role & Capability Catalog (E2)
    # Foundation-first, read-mostly API (E2 section 19/20): the catalog
    # itself is seeded/code-defined, not writable through HTTP in this
    # phase. validate-assignment is the one "action" endpoint, and it
    # answers a question (is this allowed) rather than performing one --
    # an invalid answer is still a normal 200, never a 4xx.
    @app.get("/api/engineering/roles")
    def api_list_roles():
        return roles_catalog.list_roles()
    @app.get("/api/engineering/roles/{key}")
    def api_get_role(key:str):
        role=roles_catalog.get_role(key)
        if not role: raise HTTPException(404,"Unknown engineering role")
        return {**role,"capabilities":roles_catalog.capabilities_for_role(key)}
    @app.get("/api/engineering/capabilities")
    def api_list_capabilities():
        return roles_catalog.list_capabilities()
    @app.get("/api/engineering/providers")
    def api_list_providers():
        return {p:roles_catalog.capabilities_for_provider(p) for p in roles_catalog.providers}
    @app.post("/api/engineering/validate-assignment")
    def api_validate_assignment(request:Request,provider:str=Form(...),role_key:str=Form(...),repository_id:str=Form(""), _csrf: None = Depends(_mutating_csrf)):
        # Diagnostic/advisory (see docstring below) -- VIEWER is enough
        # when a repository is named; identity only otherwise.
        _require_org_role_for_repo(
            request, int(repository_id) if repository_id.strip().isdigit() else None, "VIEWER")
        policy=None
        if repository_id.strip().isdigit():
            r=repo(int(repository_id))
            try: policy=load_engineering_policy(Path(r["repo_path"]))
            except ContractError as exc: raise GitSafetyError(str(exc))
        # Diagnostic/advisory query, not an assignment action of its own
        # (E2 section 24: never flood the audit log on a passive read) --
        # the real ROLE_ASSIGNMENT_VALIDATED/REJECTED events are logged
        # where an assignment actually takes effect (_start_builder_session,
        # start_review, start_qa).
        return roles_catalog.validate_assignment(provider,role_key,policy)
    @app.get("/api/engineering/recommended-roles")
    def api_recommended_roles(change_type:str="FEATURE",risk_level:str="NORMAL"):
        return {"change_type":change_type.upper(),"risk_level":risk_level.upper(),
                "recommended_roles":roles_catalog.recommended_roles_for_change(change_type,risk_level)}
    @app.get("/api/repositories/{rid}/engineering-policy")
    def api_repository_engineering_policy(rid:int, _authz: None = Depends(require_read_role("repository", "rid"))):
        r=repo(rid)
        try: policy=load_engineering_policy(Path(r["repo_path"]))
        except ContractError as exc: raise GitSafetyError(str(exc))
        return {"repository_id":rid,"policy":policy}

    # -------------------------------------------- Workflow / Process Engine (E3)
    def _change_row(cid:int):
        row=changes.get(cid)
        if not row: raise HTTPException(404,"Change not found")
        return row
    def _change_engineering_policy(change:dict):
        """The Change's own repository's PROJECT.yaml engineering:
        policy (E3.12), resolved via Change.project_id -- repositories
        already IS the project boundary (E1.6); None if the Change has
        no project_id or the repo declares no such block."""
        if not change.get("project_id"): return None
        r=db.one("SELECT repo_path FROM repositories WHERE id=?",(change["project_id"],))
        if not r: return None
        try: return load_engineering_policy(Path(r["repo_path"]))
        except ContractError: return None

    @app.get("/api/engineering/workflow-profiles")
    def api_list_workflow_profiles():
        return [{**p,"stages":workflow_catalog.profile_stages(p["key"])} for p in workflow_catalog.list_profiles()]
    @app.get("/api/engineering/task-types")
    def api_list_task_types():
        return workflow_catalog.list_task_types()
    @app.get("/api/engineering/task-types/{key}")
    def api_get_task_type(key:str):
        tt=workflow_catalog.get_task_type(key)
        if not tt: raise HTTPException(404,"Unknown task type")
        return tt

    @app.get("/api/changes/{cid}/workflow")
    def api_get_change_workflow(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        run=workflow_service.get_workflow(cid)
        if not run: raise HTTPException(404,"This Change has no workflow yet")
        return run
    @app.post("/api/changes/{cid}/workflow")
    def api_create_change_workflow(cid:int,profile_key:str=Form(""), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        change=_change_row(cid)
        policy=_change_engineering_policy(change)
        return workflow_service.create_workflow_for_change(cid,profile_key.strip() or None,policy)
    @app.get("/api/changes/{cid}/workflow/state")
    def api_change_workflow_state(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return workflow_service.evaluate_workflow(cid)
    @app.get("/api/changes/{cid}/ready-tasks")
    def api_change_ready_tasks(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return {"ready_tasks":workflow_service.list_ready_tasks(cid)}
    @app.get("/api/changes/{cid}/unmet-gates")
    def api_change_unmet_gates(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return {"unmet_gates":workflow_service.list_unmet_gates(cid)}

    @app.post("/api/tasks/{tid}/task-type")
    def api_set_task_type(tid:int,task_type:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Optional (E3.1) -- a Task never requires one; NULL/'' clears
        it back to unset, exactly like spec_change_classification's own
        write route. Validated against the seeded catalog, never a
        free-text value that could silently drift from it."""
        task_row(tid)
        key=task_type.strip().upper() or None
        if key and not workflow_catalog.get_task_type(key):
            raise WorkflowError(f"Unknown task type: {key}")
        db.execute("UPDATE tasks SET task_type=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(key,tid))
        db.event("task",tid,"TASK_TYPE_SET",str(key))
        return task_row(tid)

    # ---- Task dependency graph (E3.6) ---------------------------------
    @app.get("/api/tasks/{tid}/dependencies")
    def api_task_dependencies(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return {"depends_on":task_dependencies.dependencies_for(tid),
                "dependents":task_dependencies.dependents_of(tid),
                "readiness":task_dependencies.readiness(tid,decision)}
    @app.post("/api/tasks/{tid}/dependencies")
    def api_add_task_dependency(tid:int,depends_on_task_id:int=Form(...), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_dependencies.add_dependency(tid,depends_on_task_id)
        return {"depends_on":task_dependencies.dependencies_for(tid),
                "dependents":task_dependencies.dependents_of(tid),
                "readiness":task_dependencies.readiness(tid,decision)}

    # -------------------------------------------------- Dynamic Planner (E4)
    # PlannerService never launches a coding session -- see
    # app/services/planner_service.py's module docstring for the four-way
    # separation (reasoning / artifact / materialization / execution)
    # this whole route group respects.
    def _plan_row(pid:int):
        row=planner_service.get_plan(pid)
        if not row: raise HTTPException(404,"Plan not found")
        return row

    @app.post("/api/changes/{cid}/plan")
    def api_plan_change(cid:int,provider:str=Form("claude"),materialize:bool=Form(False), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        return planner_service.plan_change(cid,provider=provider,materialize=materialize)
    @app.get("/api/changes/{cid}/plans")
    def api_list_plans(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return planner_service.list_plans(cid)
    @app.post("/api/changes/{cid}/replan")
    def api_replan_change(cid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        return planner_service.replan_change(cid,provider=provider)

    @app.get("/api/plans/{pid}")
    def api_get_plan(pid:int, _authz: None = Depends(require_read_role("plan", "pid"))):
        plan=_plan_row(pid)
        return {**plan,"items":planner_service.plan_items(pid),"human_decisions":planner_service.human_decisions(pid)}
    @app.post("/api/plans/{pid}/validate")
    def api_validate_plan(pid:int, _authz: None = Depends(require_role("plan", "pid", "MEMBER"))):
        _plan_row(pid)
        return planner_service.validate_plan(pid)
    @app.post("/api/plans/{pid}/materialize")
    def api_materialize_plan(pid:int, _authz: None = Depends(require_role("plan", "pid", "MEMBER"))):
        _plan_row(pid)
        return planner_service.materialize_plan(pid)
    @app.get("/api/plans/{pid}/validation")
    def api_plan_validation(pid:int, _authz: None = Depends(require_read_role("plan", "pid"))):
        plan=_plan_row(pid)
        return json.loads(plan["validation_result"] or "{}")
    @app.get("/api/plans/{pid}/task-graph")
    def api_plan_task_graph(pid:int, _authz: None = Depends(require_read_role("plan", "pid"))):
        _plan_row(pid)
        items=planner_service.plan_items(pid)
        return [{"key":it["item_key"],"title":it["title"],"task_type":it["task_type"],
                 "preferred_role":it["preferred_role"],"depends_on":json.loads(it["depends_on_keys"] or "[]"),
                 "materialized_task_id":it["materialized_task_id"]} for it in items]
    @app.post("/api/plans/{pid}/human-decisions/{did}/resolve")
    def api_resolve_human_decision(pid:int,did:int,resolution_note:str=Form(...), _authz: None = Depends(require_role("plan", "pid", "MEMBER"))):
        _plan_row(pid)
        return planner_service.resolve_human_decision(did,resolution_note)
    @app.get("/api/plans/{pid}/staleness")
    def api_plan_staleness(pid:int, _authz: None = Depends(require_read_role("plan", "pid"))):
        _plan_row(pid)
        return planner_service.check_staleness(pid)
    @app.get("/api/plans/{pid}/design-staleness")
    def api_plan_design_staleness(pid:int, _authz: None = Depends(require_read_role("plan", "pid"))):
        """E6.17: PLAN_DESIGN_STALE -- surfaced separately from spec
        staleness above since they detect different drift (spec baseline
        vs. architecture/design WorkProduct state)."""
        _plan_row(pid)
        return planner_service.check_design_staleness(pid)
    @app.get("/api/plans/{pid}/test-design-staleness")
    def api_plan_test_design_staleness(pid:int, _authz: None = Depends(require_read_role("plan", "pid"))):
        """E7.16: PLAN_TEST_DESIGN_STALE -- surfaced separately from spec/
        design staleness above since it detects test-design drift."""
        _plan_row(pid)
        return planner_service.check_test_design_staleness(pid)
    @app.post("/api/human-decisions/{did}/resolve")
    def api_resolve_human_decision_generic(did:int,resolution_note:str=Form(...), _authz: None = Depends(require_role("human_decision", "did", "MEMBER"))):
        """E5.11: the generalized resolve path -- works for a decision
        raised against a Change, a Plan, OR a SpecProposal alike, since
        human_decisions is now one shared table/service (app/services/
        human_decisions.py). /api/plans/{pid}/human-decisions/{did}/resolve
        above stays exactly as E4 shipped it for backward compatibility."""
        return planner_service.resolve_human_decision(did,resolution_note)

    # ------------------------------------------ Autonomous Spec Lifecycle (E5)
    # SPEC AUTHORING AGENT / SPEC ARTIFACT / SPEC REVIEW AGENT / SPEC
    # APPROVAL stay strictly separate here too -- see
    # app/services/spec_lifecycle_service.py's module docstring.
    # SpecRegistry remains the one canonical truth loader; apply_proposal
    # is the ONLY place canonical specs/**/*.yaml is ever written.
    def _proposal_row(pid:int):
        row=spec_lifecycle_service.get_proposal(pid)
        if not row: raise HTTPException(404,"Spec proposal not found")
        return row

    @app.post("/api/changes/{cid}/requirements/analyze")
    def api_analyze_requirements(cid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        return requirement_analysis_service.analyze(cid,provider=provider)
    @app.get("/api/changes/{cid}/requirements")
    def api_get_requirements(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        wp=spec_lifecycle_service.get_requirement_analysis(cid)
        if not wp: raise HTTPException(404,"No Requirement Analysis yet for this Change")
        return {**wp,"content_metadata":json.loads(wp["content_metadata"] or "{}")}

    def _proposal_out(p:dict):
        """Consistent shape everywhere a proposal is returned to a
        caller -- proposed_content always a parsed object, never a raw
        JSON string a client would have to double-decode."""
        return {**p,"proposed_content":json.loads(p["proposed_content"])}

    @app.post("/api/changes/{cid}/spec-proposals")
    def api_create_spec_proposal(cid:int,requirement_analysis_id:str=Form(""),provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        ra_id=int(requirement_analysis_id) if requirement_analysis_id.strip().isdigit() else None
        if ra_id is None:
            wp=spec_lifecycle_service.get_requirement_analysis(cid)
            if not wp: raise GitSafetyError("No Requirement Analysis exists for this Change yet -- run /requirements/analyze first")
            ra_id=wp["id"]
        result=spec_author_service.author(cid,ra_id,provider=provider)
        if result["outcome"]=="READY":
            spec_lifecycle_service.validate_proposal(result["proposal"]["id"])
            result["proposal"]=_proposal_out(spec_lifecycle_service.get_proposal(result["proposal"]["id"]))
        return result
    @app.get("/api/changes/{cid}/spec-proposals")
    def api_list_spec_proposals(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return spec_lifecycle_service.list_proposals(cid)
    @app.get("/api/spec-proposals/{pid}")
    def api_get_spec_proposal(pid:int, _authz: None = Depends(require_read_role("spec_proposal", "pid"))):
        p=_proposal_row(pid)
        return {**_proposal_out(p),"human_decisions":spec_lifecycle_service.human_decisions_for(pid)}
    @app.post("/api/spec-proposals/{pid}/review")
    def api_review_spec_proposal(pid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("spec_proposal", "pid", "MEMBER"))):
        _proposal_row(pid)
        review=spec_review_service.review(pid,provider=provider)
        if review["outcome"]=="REVIEWED":
            spec_lifecycle_service.finalize_after_review(pid,review["verdict"])
        return review
    @app.post("/api/spec-proposals/{pid}/refine")
    def api_refine_spec_proposal(pid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("spec_proposal", "pid", "MEMBER"))):
        proposal=_proposal_row(pid)
        findings=json.loads(proposal["review_result"] or "{}")
        result=spec_author_service.refine(pid,findings,provider=provider)
        if result["outcome"]=="READY":
            spec_lifecycle_service.validate_proposal(result["proposal"]["id"])
            result["proposal"]=_proposal_out(spec_lifecycle_service.get_proposal(result["proposal"]["id"]))
        return result
    @app.post("/api/spec-proposals/{pid}/apply")
    def api_apply_spec_proposal(pid:int, _authz: None = Depends(require_role("spec_proposal", "pid", "MEMBER"))):
        _proposal_row(pid)
        return spec_lifecycle_service.apply_proposal(pid)
    @app.get("/api/spec-proposals/{pid}/findings")
    def api_spec_proposal_findings(pid:int, _authz: None = Depends(require_read_role("spec_proposal", "pid"))):
        p=_proposal_row(pid)
        return json.loads(p["review_result"] or "{}").get("findings",[])
    @app.get("/api/spec-proposals/{pid}/validation")
    def api_spec_proposal_validation(pid:int, _authz: None = Depends(require_read_role("spec_proposal", "pid"))):
        p=_proposal_row(pid)
        return json.loads(p["validation_result"] or "{}")

    # ------------------------------------- Architecture & Design Lifecycle (E6)
    # ARCHITECTURE ANALYSIS / ARCHITECTURE ARTIFACT / TECHNICAL DESIGN /
    # UI/UX DESIGN / DESIGN REVIEW / TASK IMPLEMENTATION stay strictly
    # separate here too -- see app/services/architecture_design_service.py's
    # module docstring. No implementation Task is ever generated by this
    # block (E6.18) -- once DESIGN_READY, the Planner (E4, above) is the
    # only thing that ever creates a Task.
    def _wp_out(wp:dict|None):
        if wp is None: return None
        return {**wp,"content_metadata":json.loads(wp["content_metadata"] or "{}")}
    def _change_policy_or_none(cid:int):
        return _change_engineering_policy(_change_row(cid))

    @app.post("/api/changes/{cid}/architecture/analyze")
    def api_architecture_analyze(cid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        result=architecture_analysis_service.analyze(cid,provider=provider)
        if result.get("work_product"): result["work_product"]=_wp_out(result["work_product"])
        return result
    @app.get("/api/changes/{cid}/architecture")
    def api_get_architecture(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        wp=architecture_analysis_service.current_for_change(cid)
        if not wp: raise HTTPException(404,"No Architecture Analysis yet for this Change")
        return _wp_out(wp)
    @app.post("/api/changes/{cid}/architecture/review")
    def api_architecture_review(cid:int,analysis_id:str=Form(""),provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        aid=int(analysis_id) if analysis_id.strip().isdigit() else None
        if aid is None:
            wp=architecture_analysis_service.current_for_change(cid)
            if not wp: raise HTTPException(404,"No Architecture Analysis exists for this Change yet -- run /architecture/analyze first")
            aid=wp["id"]
        result=architecture_review_service.review(aid,provider=provider)
        if result.get("work_product"): result["work_product"]=_wp_out(result["work_product"])
        return result
    @app.get("/api/changes/{cid}/adrs")
    def api_list_adrs(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return [_wp_out(wp) for wp in work_products.list_for_change(cid) if wp["kind"]=="ADR"]

    @app.post("/api/changes/{cid}/design/technical")
    def api_design_technical(cid:int,architecture_analysis_id:str=Form(""),provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        aid=int(architecture_analysis_id) if architecture_analysis_id.strip().isdigit() else None
        if aid is None:
            wp=architecture_analysis_service.current_for_change(cid)
            aid=wp["id"] if wp and wp["status"]=="APPROVED" else None
        result=technical_design_service.design(cid,aid,provider=provider)
        if result.get("work_product"): result["work_product"]=_wp_out(result["work_product"])
        return result
    @app.post("/api/changes/{cid}/design/ui-ux")
    def api_design_ui_ux(cid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        result=ui_ux_design_service.design(cid,provider=provider)
        if result.get("work_product"): result["work_product"]=_wp_out(result["work_product"])
        return result
    @app.get("/api/changes/{cid}/design")
    def api_get_design(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return {"technical_design":_wp_out(technical_design_service.current_for_change(cid)),
                "ui_ux_design":_wp_out(ui_ux_design_service.current_for_change(cid)),
                "ui_ux_applicability":architecture_design_service.detect_ui_ux(cid,project_policy=_change_policy_or_none(cid))}
    @app.post("/api/changes/{cid}/design/review")
    def api_design_review(cid:int,technical_design_id:str=Form(""),ui_ux_design_id:str=Form(""),provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        tid=int(technical_design_id) if technical_design_id.strip().isdigit() else None
        if tid is None:
            wp=technical_design_service.current_for_change(cid)
            if not wp: raise HTTPException(404,"No Technical Design exists for this Change yet -- run /design/technical first")
            tid=wp["id"]
        uid=int(ui_ux_design_id) if ui_ux_design_id.strip().isdigit() else None
        if uid is None:
            uwp=ui_ux_design_service.current_for_change(cid)
            uid=uwp["id"] if uwp else None
        result=design_review_service.review(tid,uid,provider=provider)
        if result.get("work_product"): result["work_product"]=_wp_out(result["work_product"])
        return result
    @app.post("/api/changes/{cid}/design/refine")
    def api_design_refine(cid:int,technical_design_id:str=Form(""),provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        tid=int(technical_design_id) if technical_design_id.strip().isdigit() else None
        if tid is None:
            wp=technical_design_service.current_for_change(cid)
            if not wp: raise HTTPException(404,"No Technical Design exists for this Change yet")
            tid=wp["id"]
        review_wp=db.one("SELECT content_metadata FROM work_products WHERE kind='DESIGN_REVIEW' AND change_id=? ORDER BY id DESC LIMIT 1",(cid,))
        findings=json.loads(review_wp["content_metadata"]) if review_wp else {}
        result=technical_design_service.refine(tid,findings,provider=provider)
        if result.get("work_product"): result["work_product"]=_wp_out(result["work_product"])
        uwp=ui_ux_design_service.current_for_change(cid)
        if uwp:
            ui_result=ui_ux_design_service.refine(uwp["id"],findings,provider=provider)
            if ui_result.get("work_product"): result["ui_ux_design"]=_wp_out(ui_result["work_product"])
        return result
    @app.get("/api/changes/{cid}/design/status")
    def api_design_status(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        s=architecture_design_service.status(cid,project_policy=_change_policy_or_none(cid))
        return {**s,"architecture_analysis":_wp_out(s["architecture_analysis"]),
                "technical_design":_wp_out(s["technical_design"]),"ui_ux_design":_wp_out(s["ui_ux_design"])}
    @app.get("/api/changes/{cid}/design/findings")
    def api_design_findings(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return architecture_design_service.design_findings(cid)

    # ------------------------------------- Test Design & Requirement Coverage (E7)
    # TEST SPECIFICATION / EXECUTABLE TEST / TEST RESULT / SPEC COMPLIANCE
    # stay strictly separate here too -- see app/services/test_design_
    # service.py's module docstring. A designed TestCaseSpec is never
    # itself evidence; no route here ever writes source/test files.
    def _tcs_row(tcs_id:int):
        row=test_case_specs_store.get(tcs_id)
        if not row: raise HTTPException(404,"TestCaseSpec not found")
        return row
    def _tcs_out(tc:dict):
        return {**tc,"requirement_ids":json.loads(tc["requirement_ids"] or "[]"),
                "acceptance_ids":json.loads(tc["acceptance_ids"] or "[]"),
                "invariant_ids":json.loads(tc["invariant_ids"] or "[]")}
    def _change_test_command(cid:int):
        change=_change_row(cid)
        if not change.get("project_id"): return None
        r=db.one("SELECT repo_path FROM repositories WHERE id=?",(change["project_id"],))
        if not r: return None
        cmd=load_command(Path(r["repo_path"]),"test")
        if not cmd: return None
        return {"command":cmd[0],"working_directory":cmd[1],"timeout_seconds":cmd[2]}

    def _test_design_result_out(result:dict):
        if result.get("test_plan") is not None: result["test_plan"]=_wp_out(result["test_plan"])
        if result.get("test_case_set") is not None: result["test_case_set"]=_wp_out(result["test_case_set"])
        if result.get("test_cases") is not None: result["test_cases"]=[_tcs_out(tc) for tc in result["test_cases"]]
        return result

    @app.post("/api/changes/{cid}/tests/design")
    def api_tests_design(cid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        result=test_design_service.design(cid,provider=provider,project_test_command=_change_test_command(cid))
        return _test_design_result_out(result)
    @app.get("/api/changes/{cid}/tests")
    def api_get_tests(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        test_set=test_design_service.current_test_case_set(cid)
        return {"test_plan":_wp_out(test_design_service.current_test_plan(cid)),
                "test_case_set":_wp_out(test_set),
                "test_cases":[_tcs_out(tc) for tc in (test_case_specs_store.list_for_work_product(test_set["id"]) if test_set else [])]}
    @app.get("/api/changes/{cid}/tests/coverage")
    def api_tests_coverage(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return requirement_coverage_service.compute(cid)
    @app.post("/api/changes/{cid}/tests/review")
    def api_tests_review(cid:int,test_case_set_id:str=Form(""),provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        sid=int(test_case_set_id) if test_case_set_id.strip().isdigit() else None
        if sid is None:
            wp=test_design_service.current_test_case_set(cid)
            if not wp: raise HTTPException(404,"No Test Design exists for this Change yet -- run /tests/design first")
            sid=wp["id"]
        result=test_review_service.review(sid,provider=provider)
        if result.get("work_product") is not None: result["work_product"]=_wp_out(result["work_product"])
        if result.get("coverage_work_product") is not None: result["coverage_work_product"]=_wp_out(result["coverage_work_product"])
        return result
    @app.post("/api/changes/{cid}/tests/refine")
    def api_tests_refine(cid:int,test_case_set_id:str=Form(""),provider:str=Form("claude"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        _change_row(cid)
        sid=int(test_case_set_id) if test_case_set_id.strip().isdigit() else None
        if sid is None:
            wp=test_design_service.current_test_case_set(cid)
            if not wp: raise HTTPException(404,"No Test Design exists for this Change yet")
            sid=wp["id"]
        review_wp=db.one("SELECT content_metadata FROM work_products WHERE kind='TEST_REVIEW' AND change_id=? ORDER BY id DESC LIMIT 1",(cid,))
        findings=json.loads(review_wp["content_metadata"]) if review_wp else {}
        result=test_design_service.refine(sid,findings,provider=provider)
        return _test_design_result_out(result)
    @app.get("/api/changes/{cid}/tests/status")
    def api_tests_status(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        s=test_design_lifecycle_service.status(cid)
        return {**s,"test_plan":_wp_out(s["test_plan"]),"test_case_set":_wp_out(s["test_case_set"])}
    @app.get("/api/changes/{cid}/tests/staleness")
    def api_tests_staleness(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        _change_row(cid)
        return test_design_lifecycle_service.staleness(cid)

    @app.get("/api/test-cases/{tcid}")
    def api_get_test_case(tcid:int, _authz: None = Depends(require_read_role("test_case_spec", "tcid"))):
        return _tcs_out(_tcs_row(tcid))
    @app.post("/api/test-cases/{tcid}/map-executable")
    def api_map_test_case_executable(tcid:int,repository_id:str=Form(""),repository_path:str=Form(...),
                                      test_symbol:str=Form(""),command:str=Form(""),framework:str=Form(""), _authz: None = Depends(require_role("test_case_spec", "tcid", "MEMBER"))):
        _tcs_row(tcid)
        rid=int(repository_id) if repository_id.strip().isdigit() else None
        return executable_test_mapping_service.map(tcid,rid,repository_path,test_symbol,command=command,framework=framework)
    @app.get("/api/test-cases/{tcid}/mapping")
    def api_get_test_case_mapping(tcid:int, _authz: None = Depends(require_read_role("test_case_spec", "tcid"))):
        _tcs_row(tcid)
        m=executable_test_mapping_service.get(tcid)
        return m if m else {"test_case_spec_id":tcid,"implementation_status":"UNIMPLEMENTED"}

    @app.post("/api/workspaces/{wid}/builder-instructions")
    def save_builder_instructions(wid:int,builder_instructions:str=Form(""), _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        """Optional, per-Builder-Workspace extra instructions layered on
        top of the Task's own effective prompt -- e.g. distinguishing what
        a Backend Builder should do from what a Firmware Builder on the
        same Task should do. Never required; empty means 'use the Task
        prompt alone'. Not separately versioned (see migration V8)."""
        w=agent_row(wid)
        db.execute("UPDATE agent_workspaces SET builder_instructions=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(builder_instructions.strip(),wid))
        db.event("agent",wid,"BUILDER_INSTRUCTIONS_SAVED")
        return RedirectResponse(f"/tasks/{w['task_id']}" if w.get("task_id") else f"/workspaces/{wid}",303)
    def add_task_workspace(tid,repository_id,agent,role,base_branch,sandbox_profile):
        """Single source for 'create one Agent Workspace inside a Task':
        used by the classic one-at-a-time /api/tasks/{tid}/workspaces route
        AND by New-Task's inline multi-workspace flow, so a failure in one
        workspace during Task creation is handled the exact same way (Task
        and any already-created workspace stay, nothing is silently rolled
        back) as adding a workspace later."""
        t=task_row(tid)
        if t["status"]=="BACKLOG": return {"ok":False,"error":"Task must be Selected for Development before creating an Agent Workspace"}
        try: r=repo(repository_id)
        except HTTPException as exc: return {"ok":False,"error":str(exc.detail)}
        agent_s=slugify(agent)
        if agent_s not in settings.agents: return {"ok":False,"error":f"Agent not allowed: {agent}"}
        # Branch/worktree name must include the repo, not just the task slug
        # (matches the same qualification already used for Integration
        # Workspaces below) -- agent_workspaces.branch is UNIQUE across the
        # WHOLE table, not scoped per repository_id, so a multi-repo Task
        # using the same agent for a second repo would otherwise compute
        # the identical branch string and fail with a raw
        # "UNIQUE constraint failed: agent_workspaces.branch" at INSERT
        # time even though each repo's own git history has no collision.
        try: branch,path,commit=git.create_agent(r["repo_path"],agent_s,f"{t['slug']}-{r['repo_name']}",base_branch)
        except (GitSafetyError,GitCommandError) as exc: return {"ok":False,"error":str(exc)}
        role_clean=role.strip()[:80]; profile_clean=sandbox_profile.strip().upper() or None
        try:
            wid=db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status,task_id,role,sandbox_profile) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (repository_id,agent_s,t["slug"],branch,str(path),base_branch,commit,commit,"CREATED",tid,role_clean,profile_clean))
        except Exception as exc:
            if not git.status(path).strip(): git.close(r["repo_path"],path)
            # Never surface a raw SQLite message (e.g. "UNIQUE constraint
            # failed: agent_workspaces.branch") to the browser -- the repo
            # qualification above already prevents the known cross-repo
            # collision; this is a last-resort, honest but readable
            # fallback for anything else that still hits the constraint.
            msg = "A workspace with this exact branch/worktree already exists for this task." if "UNIQUE constraint failed" in str(exc) else str(exc)
            return {"ok":False,"error":msg}
        db.event("agent",wid,"WORKSPACE_CREATED",branch)
        db.execute("UPDATE tasks SET updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,))
        sid=auto_create_sandbox(tid,repository_id,r["repo_path"],"AGENT_WORKSPACE",wid,role_clean or agent_s,branch,commit,path,profile_clean,t.get("default_sandbox_profile"))
        if sid: db.execute("UPDATE agent_workspaces SET sandbox_profile=(SELECT profile FROM sandboxes WHERE id=?) WHERE id=?",(sid,wid))
        return {"ok":True,"workspace_id":wid,"repo_name":r["repo_name"],"agent":agent_s}

    # Autonomous Implementation Orchestration (Phase E8): constructed
    # here, once add_task_workspace/_start_builder_session both exist --
    # these are the EXACT closures the manual "Start Builder"/"New
    # Workspace" actions already use (E8.10: no second Supervisor, no
    # second shell-execution mechanism). tick()/run_change() are never
    # called by anything in this file automatically -- only the explicit
    # API routes below, matching E8.6/E8.27's own "opt-in, manual-tick
    # only, no background daemon" requirement.
    worktree_manager=WorktreeManager(db,git,add_task_workspace)
    app.state.worktree_manager=worktree_manager
    autonomous_execution_service=AutonomousExecutionService(
        db,changes,work_products,decision,task_dependencies,workflow_service,human_decisions,spec_gate,
        roles_catalog,planner_service,git,add_task_workspace,_start_builder_session,
        _resolve_project_policy_for_change,settings,test_case_specs=test_case_specs_store,
        worktree_manager=worktree_manager)
    app.state.autonomous_execution_service=autonomous_execution_service

    # ---- E9: Independent Code Review, Security Review & Fix Loop -----
    findings_store=FindingsStore(db)
    app.state.findings_store=findings_store
    code_review_service=CodeReviewService(
        db,changes,work_products,findings_store,planner_invoker,roles_catalog,git,
        worktree_manager,task_execution_context_builder,human_decisions,_resolve_project_policy_for_change)
    app.state.code_review_service=code_review_service
    security_applicability_service=SecurityApplicabilityService(db,git)
    app.state.security_applicability_service=security_applicability_service
    security_review_service=SecurityReviewService(
        db,changes,work_products,findings_store,planner_invoker,roles_catalog,git,
        worktree_manager,task_execution_context_builder,human_decisions,_resolve_project_policy_for_change)
    app.state.security_review_service=security_review_service
    review_fix_orchestrator=ReviewFixOrchestratorService(
        db,changes,work_products,findings_store,code_review_service,security_review_service,
        security_applicability_service,worktree_manager,workflow_service,human_decisions,decision,git,
        _start_builder_session,_resolve_project_policy_for_change)
    app.state.review_fix_orchestrator=review_fix_orchestrator
    # E9.23/E9.24/E9.25: same additive-hook pattern E6/E7 already used
    # for architecture_design_gate/test_design_gate -- REVIEW_PASS/
    # SECURITY_PASS now consult real E9 evidence when it exists, fall
    # back to the exact legacy per-workspace check otherwise.
    workflow_service.review_gate=review_fix_orchestrator

    # ---- E10: Integration, Release, Deploy & Runtime Verification ----
    integration_service=IntegrationService(db,work_products,worktree_manager,review_fix_orchestrator,git)
    app.state.integration_service=integration_service
    release_service=ReleaseService(db,changes,work_products,workflow_service,human_decisions,deployer,
        _resolve_project_policy_for_change)
    app.state.release_service=release_service
    # E10.23: same additive-hook pattern as review_gate above --
    # DEPLOY_VERIFIED now consults real Release/runtime evidence when
    # it exists, falls back to the exact legacy DEV-only check otherwise.
    workflow_service.deploy_verified_gate=release_service
    # E10.28/E10.29: wire the Release/Deploy tabs + Change Overview
    # summary onto real evidence -- same additive-attribute pattern,
    # change_control_surface was constructed earlier in create_app()
    # before these two services existed.
    change_control_surface.integration_service=integration_service
    change_control_surface.release_service=release_service

    # ---- E11: Human Product Acceptance & Production Outcome Review ----
    product_acceptance_service=ProductAcceptanceService(
        db,changes,work_products,release_service,architecture_design_service,
        test_case_specs_store,human_decisions,workflow_service,_resolve_project_policy_for_change)
    app.state.product_acceptance_service=product_acceptance_service
    # E11.13: same additive-hook pattern as review_gate/deploy_verified_gate
    # above -- HUMAN_ACCEPTANCE now consults real ProductAcceptance
    # evidence, falls back to the exact legacy approved-HUMAN_DECISION
    # check otherwise.
    workflow_service.human_acceptance_gate=product_acceptance_service
    change_control_surface.product_acceptance_service=product_acceptance_service

    # Track A1.3: ChangeListSummaryService -- composed here, same
    # construction-order reason as autonomous_execution_service on the
    # Change Detail route above (product_acceptance_service doesn't exist
    # yet earlier in create_app()). GET /changes resolves the name at
    # request time, well after create_app() has finished running.
    change_list_summary_service=ChangeListSummaryService(
        db,changes,workflow_service,human_decisions,product_acceptance_service)
    app.state.change_list_summary_service=change_list_summary_service

    # Track A1.11-A1.18/A1.23: Simple Mode composition -- presentation
    # only, over change_control_surface's own already-composed truth
    # (see that service's and simple_view_service.py's own docstrings).
    simple_view_service=SimpleViewService(db,change_control_surface,changes)
    app.state.simple_view_service=simple_view_service

    # ---- E12: Bug / Incident Closed Loop -------------------------------
    incident_service=IncidentService(
        db,changes,work_products,trace,spec_lifecycle_service,test_case_specs_store,
        workflow_service,release_service,specs_root)
    app.state.incident_service=incident_service

    # ---- E13: Parallel Multi-Agent Execution & Integration Waves ------
    parallel_safety_service=ParallelSafetyService(db,task_dependencies,_resolve_project_policy_for_change)
    app.state.parallel_safety_service=parallel_safety_service
    execution_wave_service=ExecutionWaveService(
        db,changes,autonomous_execution_service,parallel_safety_service,integration_service,git,
        _resolve_project_policy_for_change)
    app.state.execution_wave_service=execution_wave_service

    @app.get("/api/changes/{cid}/autonomous-execution")
    def api_autonomous_execution_status(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return autonomous_execution_service.status(cid)
    @app.get("/api/changes/{cid}/auto-ready-tasks")
    def api_auto_ready_tasks(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        return autonomous_execution_service.list_auto_ready_tasks(cid)
    @app.post("/api/changes/{cid}/autonomous-execution/tick")
    def api_autonomous_execution_tick(cid:int, _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        change_row(cid)
        return autonomous_execution_service.tick(cid)
    @app.post("/api/tasks/{tid}/autonomous-start")
    def api_task_autonomous_start(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Operator-triggered single-task run (E8.23's 'Run next ready
        task' UI action) -- evaluates THIS Task specifically rather than
        letting tick() pick whichever is first in DAG order, so an
        operator testing one Task doesn't accidentally launch a
        different one."""
        task_row(tid)
        return autonomous_execution_service.launch_task_if_ready(tid)
    @app.get("/api/tasks/{tid}/execution-readiness")
    def api_task_execution_readiness(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return autonomous_execution_service.evaluate_task(tid)

    # ---- E8.5 Worktree Isolation Foundation --------------------------
    @app.get("/api/tasks/{tid}/worktree")
    def api_task_worktree(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        ws=worktree_manager.get_task_worktree(tid)
        if not ws: raise HTTPException(404,"This Task has no managed worktree")
        return ws
    @app.post("/api/tasks/{tid}/worktree/create")
    def api_task_worktree_create(tid:int,repository_id:int=Form(...),agent:str=Form(...),role:str=Form(""),base_branch:str=Form(""),sandbox_profile:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Diagnostic/manual-testing entry point (E8.5.26) -- autonomous
        execution normally creates a worktree internally via the exact
        same add_task_workspace() call this delegates to."""
        task_row(tid)
        return worktree_manager.create_task_worktree(tid,repository_id,agent,role,base_branch or None,sandbox_profile)
    @app.get("/api/repositories/{rid}/worktrees")
    def api_repository_worktrees(rid:int, _authz: None = Depends(require_read_role("repository", "rid"))):
        repo(rid)
        return worktree_manager.list_repository_worktrees(rid)
    @app.post("/api/tasks/{tid}/worktree/inspect")
    def api_task_worktree_inspect(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        return worktree_manager.inspect_task_worktree(tid)
    @app.post("/api/tasks/{tid}/worktree/abandon")
    def api_task_worktree_abandon(tid:int,note:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        return worktree_manager.abandon_task_worktree(tid,note)
    @app.post("/api/tasks/{tid}/worktree/cleanup")
    def api_task_worktree_cleanup(tid:int,force:bool=Form(False), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        return worktree_manager.remove_task_worktree(tid,force)
    @app.get("/api/tasks/{tid}/integration-check")
    def api_task_integration_check(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return worktree_manager.check_integration(tid)

    # ---- E9 Independent Review / Security Review / Fix Loop ---------
    @app.post("/api/tasks/{tid}/review/code")
    def api_task_review_code(tid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        return code_review_service.review_task(tid,provider)
    @app.post("/api/tasks/{tid}/review/security")
    def api_task_review_security(tid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        return security_review_service.review_task(tid,provider)
    @app.get("/api/tasks/{tid}/reviews")
    def api_task_reviews(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return db.all("SELECT * FROM review_runs WHERE task_id=? ORDER BY id DESC",(tid,))
    @app.get("/api/tasks/{tid}/findings")
    def api_task_findings(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return findings_store.list_for_task(task_chain_ids(db,tid))
    @app.post("/api/tasks/{tid}/review-fix/tick")
    def api_task_review_fix_tick(tid:int,provider:str=Form("claude"), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        return review_fix_orchestrator.tick(tid,provider)
    @app.get("/api/tasks/{tid}/review-fix/status")
    def api_task_review_fix_status(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return review_fix_orchestrator.status(tid)
    @app.post("/api/findings/{fid}/resolve")
    def api_finding_resolve(fid:int,resolution_reference:str=Form(...),status:str=Form("RESOLVED"), _authz: None = Depends(require_role("finding", "fid", "MEMBER"))):
        return findings_store.resolve(fid,resolution_reference,status)
    @app.get("/api/tasks/{tid}/integration-readiness")
    def api_task_integration_readiness(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return review_fix_orchestrator.integration_readiness(tid)

    # ---- E10 Integration / Release / Deploy / Rollback ----------------
    @app.post("/api/tasks/{tid}/integrate")
    def api_task_integrate(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        return integration_service.integrate_task(tid)
    @app.get("/api/tasks/{tid}/integration")
    def api_task_integration_status(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return integration_service.preflight_integration(tid)

    @app.post("/api/releases")
    def api_create_release(request:Request,repository_id:int=Form(...),task_ids:str=Form(...),version:str=Form(""), _csrf: None = Depends(_mutating_csrf)):
        _require_org_role_for_repo(request, repository_id, "MEMBER")
        ids=[int(x) for x in task_ids.split(",") if x.strip()]
        r=release_service.create_release(repository_id,ids,version.strip() or None)
        return r
    @app.get("/api/releases")
    def api_list_releases(request:Request,repository_id:int):
        _require_org_role_for_repo(request,repository_id,"VIEWER")
        return release_service.list_for_repository(repository_id)
    @app.get("/api/releases/{rid}")
    def api_get_release(rid:int, _authz: None = Depends(require_read_role("release", "rid"))):
        r=release_service.get(rid)
        if not r: raise HTTPException(404,"Release not found")
        return {**r,"tasks":release_service.tasks_for(rid)}

    @app.post("/api/releases/{rid}/build")
    def api_release_build(rid:int, _authz: None = Depends(require_role("release", "rid", "MEMBER"))):
        return release_service.build(rid)
    @app.post("/api/releases/{rid}/qualify")
    def api_release_qualify(rid:int, _authz: None = Depends(require_role("release", "rid", "MEMBER"))):
        return release_service.qualify(rid,review_fix_orchestrator)

    @app.post("/api/releases/{rid}/deploy/test")
    def api_release_deploy_test(rid:int, _authz: None = Depends(require_role("release", "rid", "MEMBER"))):
        return release_service.deploy_test(rid)
    @app.post("/api/releases/{rid}/deploy/production")
    def api_release_deploy_production(rid:int, _authz: None = Depends(require_role("release", "rid", "ADMIN"))):
        return release_service.deploy_production(rid)
    @app.post("/api/releases/{rid}/approve-production")
    def api_release_approve_production(rid:int,approved_by:str=Form(...), _authz: None = Depends(require_role("release", "rid", "ADMIN"))):
        return release_service.approve_production(rid,approved_by)
    @app.post("/api/releases/{rid}/tick")
    def api_release_tick(rid:int, _authz: None = Depends(require_role("release", "rid", "MEMBER"))):
        return release_service.release_tick(rid,review_fix_orchestrator)

    @app.get("/api/releases/{rid}/deployments")
    def api_release_deployments(rid:int, _authz: None = Depends(require_read_role("release", "rid"))):
        r=release_service.get(rid)
        if not r: raise HTTPException(404,"Release not found")
        ids=[x for x in (r["test_deployment_id"],r["production_deployment_id"]) if x]
        return [db.one("SELECT * FROM deployments WHERE id=?",(i,)) for i in ids]
    @app.get("/api/releases/{rid}/runtime-verification")
    def api_release_runtime_verification(rid:int, _authz: None = Depends(require_read_role("release", "rid"))):
        r=release_service.get(rid)
        if not r: raise HTTPException(404,"Release not found")
        return db.all(
            "SELECT * FROM work_products WHERE kind='RUNTIME_VERIFICATION' AND json_extract(content_metadata,'$.release_id')=? ORDER BY id DESC",(rid,))

    # E10.19: a dedicated release-scoped rollback route -- the existing
    # /api/deployments/{did}/rollback (below, unchanged) stays the exact
    # legacy per-Task DEV form/redirect action; a Release's own
    # rollback is a distinct JSON API action, never overloaded onto the
    # same path with a different response shape.
    @app.post("/api/releases/{rid}/rollback")
    def api_release_rollback(rid:int, _authz: None = Depends(require_role("release", "rid", "ADMIN"))):
        r=release_service.get(rid)
        if not r: raise HTTPException(404,"Release not found")
        return release_service.rollback_production(rid)

    # ---- E11.21: Human Product Acceptance API ----------------------
    @app.get("/api/changes/{cid}/acceptance")
    def api_change_acceptance(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        pa=product_acceptance_service.get_current_for_change(cid)
        return {"eligibility":product_acceptance_service.eligibility(cid),
                "acceptance":pa,"checklist":product_acceptance_service.checklist(pa["id"]) if pa else [],
                "context":product_acceptance_service.context(cid)}
    @app.post("/api/changes/{cid}/acceptance/request")
    def api_change_acceptance_request(cid:int,requested_by:str=Form("human"), _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        change_row(cid)
        return product_acceptance_service.request(cid,requested_by=requested_by)
    @app.post("/api/product-acceptances/{paid}/checklist/{item_id}")
    def api_acceptance_checklist_item(paid:int,item_id:int,status:str=Form(...),note:str=Form(""),checked_by:str=Form("human"), _authz: None = Depends(require_role("product_acceptance", "paid", "MEMBER"))):
        return product_acceptance_service.check_item(paid,item_id,status,note,checked_by)
    @app.post("/api/product-acceptances/{paid}/accept")
    def api_acceptance_accept(paid:int,actor:str=Form("human"),note:str=Form(""), _authz: None = Depends(require_role("product_acceptance", "paid", "MEMBER"))):
        return product_acceptance_service.accept(paid,actor,note)
    @app.post("/api/product-acceptances/{paid}/request-change")
    def api_acceptance_request_change(paid:int,actor:str=Form("human"),feedback:str=Form(...),classification:str=Form("PRODUCT_ADJUSTMENT"), _authz: None = Depends(require_role("product_acceptance", "paid", "MEMBER"))):
        return product_acceptance_service.request_change(paid,actor,feedback,classification)
    @app.post("/api/product-acceptances/{paid}/reject")
    def api_acceptance_reject(paid:int,actor:str=Form("human"),reason:str=Form(...),classification:str=Form(""), _authz: None = Depends(require_role("product_acceptance", "paid", "MEMBER"))):
        return product_acceptance_service.reject(paid,actor,reason,classification.strip() or None)
    @app.get("/api/product-acceptances/{paid}/evidence")
    def api_acceptance_evidence(paid:int, _authz: None = Depends(require_read_role("product_acceptance", "paid"))):
        return product_acceptance_service.evidence(paid)

    # ---- E12: Bug / Incident Closed Loop API ----------------------
    def incident_row(iid:int):
        row=incident_service.get(iid)
        if not row: raise HTTPException(404,"Incident not found")
        return row
    @app.post("/api/incidents")
    def api_report_incident(request:Request,title:str=Form(...),description:str=Form(""),source:str=Form("MANUAL"),
                             severity:str=Form("MEDIUM"),reported_by:str=Form("system"),project_id:str=Form(""), _csrf: None = Depends(_mutating_csrf)):
        pid=int(project_id) if project_id.strip().isdigit() else None
        _require_org_role_for_repo(request, pid, "MEMBER")
        return incident_service.report(title,description,source,severity,reported_by,pid)
    @app.get("/api/incidents")
    def api_list_incidents(request:Request,project_id:str="",status:str=""):
        pid=int(project_id) if project_id.strip().isdigit() else None
        return _filter_polymorphic(request,"incident",incident_service.list(project_id=pid,status=status.strip() or None))
    @app.get("/api/incidents/{iid}")
    def api_get_incident(iid:int, _authz: None = Depends(require_read_role("incident", "iid"))):
        row=incident_row(iid)
        return {**row,"regression_history":incident_service.regression_history(iid)}
    @app.post("/api/incidents/{iid}/classify")
    def api_classify_incident(iid:int,classification:str=Form(...),severity:str=Form(""), _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        return incident_service.classify(iid,classification,severity.strip() or None)
    @app.post("/api/incidents/{iid}/link-spec")
    def api_link_incident_spec(iid:int,feature_id:str=Form(...),requirement_ids:str=Form(""),acceptance_ids:str=Form(""), _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        def _ids(raw): return [x.strip() for x in raw.replace(",","\n").splitlines() if x.strip()]
        return incident_service.link_spec(iid,feature_id,_ids(requirement_ids),_ids(acceptance_ids))
    @app.post("/api/incidents/{iid}/spec-gap")
    def api_incident_spec_gap(iid:int,note:str=Form(""), _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        return incident_service.mark_spec_gap(iid,note)
    @app.post("/api/incidents/{iid}/sync")
    def api_sync_incident(iid:int, _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        incident_service.sync_spec_gap(iid)
        return incident_service.sync_status(iid)
    @app.post("/api/incidents/{iid}/reproduction/start")
    def api_incident_reproduction_start(iid:int, _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        return incident_service.start_reproduction(iid)
    @app.post("/api/incidents/{iid}/reproduction/record")
    def api_incident_reproduction_record(iid:int,reproduced:bool=Form(...),note:str=Form(""),commit:str=Form(""), _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        return incident_service.record_reproduction(iid,reproduced,note,commit.strip() or None)
    @app.post("/api/incidents/{iid}/regression-test")
    def api_incident_regression_test(iid:int,test_case_spec_id:int=Form(...), _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        return incident_service.add_regression_test(iid,test_case_spec_id)
    @app.post("/api/incidents/{iid}/regression-result")
    def api_incident_regression_result(iid:int,status:str=Form(...),tested_commit:str=Form(...),command:str=Form(""), _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        return incident_service.record_regression_result(iid,status,tested_commit,command)
    @app.post("/api/incidents/{iid}/verify-resolved")
    def api_incident_verify_resolved(iid:int, _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        return incident_service.verify_resolved(iid)
    @app.post("/api/incidents/{iid}/close")
    def api_incident_close(iid:int,closed_by:str=Form("human"),note:str=Form(""), _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        return incident_service.close(iid,closed_by,note)
    @app.post("/api/incidents/{iid}/reopen")
    def api_incident_reopen(iid:int,reason:str=Form(...), _authz: None = Depends(require_role("incident", "iid", "MEMBER"))):
        incident_row(iid)
        return incident_service.reopen(iid,reason)

    # ---- E13.40: Parallel Execution Wave API ----------------------------
    @app.get("/api/changes/{cid}/execution-wave")
    def api_get_execution_wave(cid:int, _authz: None = Depends(require_read_role("change", "cid"))):
        change_row(cid)
        current=execution_wave_service.current_wave_for_change(cid)
        return {"current_wave":current,"plan":execution_wave_service.plan_execution_wave(cid)}
    @app.post("/api/changes/{cid}/execution-wave/plan")
    def api_plan_execution_wave(cid:int, _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        change_row(cid)
        return execution_wave_service.plan_execution_wave(cid)
    @app.post("/api/changes/{cid}/execution-wave/run")
    def api_run_execution_wave(cid:int, _authz: None = Depends(require_role("change", "cid", "MEMBER"))):
        change_row(cid)
        return execution_wave_service.run_execution_wave(cid)
    @app.post("/api/execution-waves/{wid}/integrate")
    def api_integrate_execution_wave(wid:int, _authz: None = Depends(require_role("execution_wave", "wid", "MEMBER"))):
        if not execution_wave_service.get_wave(wid): raise HTTPException(404,"Execution wave not found")
        return execution_wave_service.integrate_wave(wid)
    @app.get("/api/execution-waves/{wid}")
    def api_get_execution_wave_detail(wid:int, _authz: None = Depends(require_read_role("execution_wave", "wid"))):
        wave=execution_wave_service.get_wave(wid)
        if not wave: raise HTTPException(404,"Execution wave not found")
        return wave
    @app.get("/execution-waves/{wid}",response_class=HTMLResponse)
    def execution_wave_detail_page(request:Request,wid:int, _authz: None = Depends(require_read_role("execution_wave", "wid"))):
        wave=execution_wave_service.get_wave(wid)
        if not wave: raise HTTPException(404,"Execution wave not found")
        change=changes.get(wave["change_id"])
        # E13.39: pairwise safety + actual-scope evidence for the tasks
        # that actually launched -- collapsible raw technical evidence,
        # composition only.
        launched=[t for t in wave["tasks"] if t["reservation_state"]=="LAUNCHED"]
        pairwise=[]
        for i in range(len(launched)):
            for j in range(i+1,len(launched)):
                pairwise.append({"a":launched[i],"b":launched[j],
                                  **parallel_safety_service.evaluate_pair(launched[i]["task_id"],launched[j]["task_id"])})
        return render(request,"execution_wave_detail.html",wave=wave,change=change,pairwise=pairwise)
    @app.get("/api/tasks/{tid}/parallel-safety")
    def api_task_parallel_safety(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        task_row(tid)
        return parallel_safety_service.conflicts_for_task(tid)
    @app.get("/api/tasks/{tid}/parallel-conflicts")
    def api_task_parallel_conflicts(tid:int, _authz: None = Depends(require_read_role("task", "tid"))):
        t=task_row(tid)
        if not t.get("change_id"): return {"task_id":tid,"conflicts":[]}
        siblings=[s for s in changes.list_tasks_for_change(t["change_id"]) if s["id"]!=tid]
        conflicts=[]
        for s in siblings:
            result=parallel_safety_service.evaluate_pair(tid,s["id"])
            if result["result"]!="PARALLEL_SAFE":
                conflicts.append({"task_id":s["id"],"title":s["title"],**result})
        return {"task_id":tid,"conflicts":conflicts}

    @app.post("/api/tasks/{tid}/workspaces")
    def create_task_workspace(tid:int,repository_id:int=Form(...),agent:str=Form(...),role:str=Form(""),base_branch:str=Form("main"),sandbox_profile:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        result=add_task_workspace(tid,repository_id,agent,role,base_branch,sandbox_profile)
        if not result["ok"]: raise GitSafetyError(result["error"])
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/setup-and-start")
    def setup_and_start(tid:int,repository_id:str=Form(""),agent:str=Form(""),role:str=Form(""),base_branch:str=Form("main"),sandbox_profile:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """The Wizard's SETUP step single primary action ('Start Claude' /
        'Start Codex', section 4): Select for Development if still in
        BACKLOG, create the Builder Workspace if one doesn't already
        exist for this repo+agent pairing, then immediately start its
        AgentSession -- one click covers workspace allocation through a
        live running agent. The user never has to separately visit Agent
        Workspaces. With no repository/agent given, starts every
        NOT_STARTED existing Builder Workspace instead (covers 'workspace
        already exists, just press Start' and the multi-Builder case)."""
        t=task_row(tid)
        if t["status"]=="BACKLOG":
            db.execute("UPDATE tasks SET status='ACTIVE',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_SELECTED")
        w=None
        if repository_id.strip() and agent.strip():
            rid=int(repository_id); agent_s=slugify(agent)
            existing=[x for x in task_workspaces(tid) if x["repository_id"]==rid and x["agent"]==agent_s]
            if existing:
                w=existing[0]
            else:
                result=add_task_workspace(tid,rid,agent,role,base_branch or "main",sandbox_profile)
                if not result["ok"]: raise GitSafetyError(result["error"])
                w=[x for x in task_workspaces(tid) if x["repository_id"]==rid and x["agent"]==agent_s][-1]
        if w is not None:
            try: _resume_builder_session(w)
            except SessionError as exc: raise GitSafetyError(str(exc)) from exc
        else:
            for b in decision.evaluate(tid)["builders"]:
                if b["agent_status"]=="NOT_STARTED":
                    try: _start_builder_session(agent_row(b["id"]))
                    except SessionError as exc: db.event("agent",b["id"],"SESSION_START_FAILED",str(exc))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/integrations")
    def create_task_integration(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Integration eligibility (section 22): only once every Builder
        Workspace is READY with a current, PASS review and nothing
        unresolved -- decision.evaluate() is the one gate, never a looser
        'has at least one READY workspace' check a route computes itself.

        Idempotency guard (real incident, PR #30): this route was not
        safe to call twice. It used to INSERT the parent task_integrations
        row, THEN attempt git.create_integration() with a deterministic
        branch name (`{task_slug}-{repo_name}`) -- a duplicate click
        (button lingering during a slow first request, a double-tap)
        re-ran it while the first call's branch already existed on disk,
        git correctly refused with "branch already exists", and the
        already-committed parent row was left orphaned with no
        integration_workspaces child. decision.task_integration() always
        reads the LATEST row for the task, so that empty orphan silently
        shadowed the real, working integration until someone noticed and
        manually deleted it. Create Integration is a one-shot creation
        action: once one exists for this Task, this route is a no-op back
        to the Task page, which already shows the real integration --
        never a second attempt at the same branch name."""
        t=task_row(tid)
        d=decision.evaluate(tid)
        if d["task_integration"] is not None:
            return RedirectResponse(f"/tasks/{tid}",303)
        if not d["integration_eligibility"]["eligible"]:
            raise GitSafetyError("Not eligible for Integration yet: every Builder Workspace must be READY with a current, PASS review")
        ready=[w for w in task_workspaces(tid) if w["status"]=="READY"]
        tiid=db.execute("INSERT INTO task_integrations(task_id,status) VALUES(?,?)",(tid,"MERGING"))
        # tasks.status stays ACTIVE -- INTEGRATING/TESTING are Task Stage
        # values now, computed live by TaskDecisionService, never persisted
        # to this column (section 3/31).
        db.event("task",tid,"INTEGRATION_CREATED",str(tiid))
        by_repo={}
        for w in ready: by_repo.setdefault(w["repository_id"],[]).append(w)
        overall_conflict=False; repo_rows=[]
        for repository_id,sources in by_repo.items():
            r=repo(repository_id)
            # branch names are unique per-repo in git, but integration_workspaces.branch
            # is a single global-unique DB column across every managed repo --
            # qualify with the repo slug so two repos in the same Task never collide.
            branch,path,commit=git.create_integration(r["repo_path"],f"{t['slug']}-{r['repo_name']}","main")
            iid=db.execute("INSERT INTO integration_workspaces(repository_id,name,branch,worktree_path,base_branch,base_commit,status,task_integration_id) VALUES(?,?,?,?,?,?,?,?)",
                           (repository_id,t["slug"],branch,str(path),"main",commit,"MERGING",tiid))
            db.event("integration",iid,"INTEGRATION_CREATED",branch); conflict=False; pinned_commit=commit; pinned_branch=branch
            for source in sources:
                result=git.merge(path,source["branch"]); current=git.head(r["repo_path"],source["branch"])
                db.execute("INSERT INTO integration_sources(integration_id,workspace_id,merged_commit,merged_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",(iid,source["id"],current))
                if result.returncode:
                    db.execute("UPDATE integration_workspaces SET status='CONFLICT' WHERE id=?",(iid,)); db.event("integration",iid,"MERGE_CONFLICT",source["branch"]); conflict=True; break
                db.event("integration",iid,"BRANCH_MERGED",source["branch"])
                # Sandbox pinning must track the ORIGINAL source branch's own
                # commit (what "the branch changed" means for staleness, docs
                # section 29), never the integration branch's own --no-ff
                # merge commit hash -- those never match even when nothing
                # about the source has changed. With >1 source per repo this
                # is the last one merged; combining multiple same-repo
                # sources into a single pinned identity is a known V1
                # simplification (see docs/CI_CD note in report).
                pinned_commit=current; pinned_branch=source["branch"]
            db.execute("UPDATE integration_workspaces SET status=? WHERE id=?",("CONFLICT" if conflict else "TESTING",iid))
            overall_conflict=overall_conflict or conflict
            repo_rows.append({"repository_id":repository_id,"integration_id":iid,"repo_path":r["repo_path"],"worktree_path":str(path),"branch":pinned_branch,"pinned_commit":pinned_commit})
        if overall_conflict:
            db.execute("UPDATE task_integrations SET status='CONFLICT',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tiid,))
            return RedirectResponse(f"/tasks/{tid}",303)
        db.execute("UPDATE task_integrations SET status='TESTING',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tiid,))
        provider=None; extra=[]
        for row_ in repo_rows:
            try: contract=load_sandbox_contract(Path(row_["repo_path"]))
            except SandboxContractError: contract=None
            if contract and provider is None: provider={**row_,"contract":contract}
            else: extra.append(row_)
        if provider:
            role_for=lambda repository_id: next((w["role"] or w["agent"] for w in ready if w["repository_id"]==repository_id),str(repository_id))
            wanted=provider["contract"].get("integration_profile") or resolve_profile(provider["contract"],None,t.get("default_sandbox_profile"))
            provider_source=SourceSpec(repository_id=provider["repository_id"],role=role_for(provider["repository_id"]),branch=provider["branch"],commit_sha=provider["pinned_commit"],worktree_path=provider["worktree_path"],repo_path=provider["repo_path"],source_type="TASK_INTEGRATION")
            extra_sources=[SourceSpec(repository_id=x["repository_id"],role=role_for(x["repository_id"]),branch=x["branch"],commit_sha=x["pinned_commit"],worktree_path=x["worktree_path"],repo_path=x["repo_path"],source_type="TASK_INTEGRATION") for x in extra]
            try:
                sid=sandboxes.create(task_id=tid,owner_type="TASK_INTEGRATION",owner_id=tiid,profile=wanted,provider=provider_source,extra_sources=extra_sources)
                if sid:
                    try: sandboxes.provision(sid)
                    except SandboxError: pass
            except SandboxContractError:
                db.event("task",tid,"SANDBOX_CONTRACT_REQUIRED","integration profile not runnable")
        else:
            db.event("task",tid,"SANDBOX_CONTRACT_REQUIRED","no participating repo declares sandbox: contract")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/verification-sandbox")
    def create_verification_sandbox(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """[Create Verification Sandbox] (section 11-13): post-completion
        'view the running app' fallback for when Integration creation
        never provisioned one (contract added later, earlier provision
        attempt failed, etc). Reuses the exact registered Integration
        Workspace worktree(s) already on disk -- never checks out a
        fresh commit or accepts one from the browser -- pinned to
        whatever that worktree's real current HEAD is right now (the
        same 'pin to the registered worktree's live HEAD' pattern every
        other sandbox in this app already uses). A sandbox already
        existing for this task_integration is left alone (idempotent,
        never a duplicate)."""
        t=task_row(tid); ti=task_integration_row(tid)
        if not ti: raise GitSafetyError("No Integration exists for this Task yet")
        if db.one("SELECT id FROM sandboxes WHERE owner_type='TASK_INTEGRATION' AND owner_id=?",(ti["id"],)):
            return RedirectResponse(f"/tasks/{tid}",303)
        ti_repos=decision.integration_repos(ti["id"])
        if not ti_repos: raise GitSafetyError("No Integration Workspace to build a sandbox from")
        rows=[]
        for r_ in ti_repos:
            try: head=git.head(r_["worktree_path"])
            except Exception: continue
            rows.append({"repository_id":r_["repository_id"],"repo_path":repo(r_["repository_id"])["repo_path"],
                         "worktree_path":r_["worktree_path"],"branch":r_["branch"],"pinned_commit":head})
        provider=None; extra=[]
        for row_ in rows:
            try: contract=load_sandbox_contract(Path(row_["repo_path"]))
            except SandboxContractError: contract=None
            if contract and provider is None: provider={**row_,"contract":contract}
            else: extra.append(row_)
        if not provider:
            raise GitSafetyError("No participating repository declares a sandbox: contract")
        role_for=lambda repository_id: repo(repository_id)["repo_name"]
        wanted=provider["contract"].get("integration_profile") or resolve_profile(provider["contract"],None,t.get("default_sandbox_profile"))
        provider_source=SourceSpec(repository_id=provider["repository_id"],role=role_for(provider["repository_id"]),branch=provider["branch"],commit_sha=provider["pinned_commit"],worktree_path=provider["worktree_path"],repo_path=provider["repo_path"],source_type="TASK_INTEGRATION")
        extra_sources=[SourceSpec(repository_id=x["repository_id"],role=role_for(x["repository_id"]),branch=x["branch"],commit_sha=x["pinned_commit"],worktree_path=x["worktree_path"],repo_path=x["repo_path"],source_type="TASK_INTEGRATION") for x in extra]
        sid=sandboxes.create(task_id=tid,owner_type="TASK_INTEGRATION",owner_id=ti["id"],profile=wanted,provider=provider_source,extra_sources=extra_sources)
        if sid: sandboxes.provision(sid)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/mark-merged")
    def mark_merge_record(tid:int,repository_id:int,pr_ref:str=Form(""),merged_commit:str=Form(""), _authz: None = Depends(require_role("repository", "repository_id", "MEMBER"))):
        """Per-repository merge tracking (section 25): a cross-repo Task
        does NOT become DONE just because one repo's PR landed --
        MergeRecord is the fact TaskDecisionService actually checks. If no
        commit is given, pin whatever the repo's Task Integration branch
        (or, absent Integration, the Builder's own branch) is at right
        now, never a guess."""
        task_row(tid); decision.merge_records(tid)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row: raise GitSafetyError("No MergeRecord for this repository on this Task")
        commit=merged_commit.strip()
        if not commit:
            ti=task_integration_row(tid)
            ir=db.one("SELECT worktree_path FROM integration_workspaces WHERE task_integration_id=? AND repository_id=?",(ti["id"],repository_id)) if ti else None
            w=db.one("SELECT worktree_path FROM agent_workspaces WHERE task_id=? AND repository_id=? ORDER BY id DESC LIMIT 1",(tid,repository_id))
            path=(ir or w or {}).get("worktree_path")
            commit=git.head(path) if path else None
        db.execute("UPDATE merge_records SET merge_status='MERGED',merged_commit=?,pr_ref=?,merged_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(commit,pr_ref.strip() or row["pr_ref"],row["id"]))
        db.event("task",tid,"MERGE_RECORDED",f"repo={repository_id} commit={commit}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/mark-pr-open")
    def mark_merge_pr_open(tid:int,repository_id:int,pr_ref:str=Form(""), _authz: None = Depends(require_role("repository", "repository_id", "MEMBER"))):
        task_row(tid); decision.merge_records(tid)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row: raise GitSafetyError("No MergeRecord for this repository on this Task")
        db.execute("UPDATE merge_records SET merge_status='PR_OPEN',pr_ref=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(pr_ref.strip(),row["id"]))
        db.event("task",tid,"MERGE_PR_OPEN",f"repo={repository_id}")
        return RedirectResponse(f"/tasks/{tid}",303)
    def _schedule_cleanup_if_done(tid):
        """Section 14: Task DONE only ever falls out of every required
        MergeRecord being MERGED (TaskDecisionService computes it, never
        set directly here) -- but the moment that becomes true, sandbox
        retention starts counting down. Never deletes a worktree/branch
        itself (section 14/15). Also the one place TASK_COMPLETED gets
        emitted (section 15/16): idempotent -- a Task that is already
        DONE and already has a TASK_COMPLETED event never gets a second
        one on a later Refresh/merge/reconcile, and re-entering DONE
        never resets cleanup_eligible_at (mark_cleanup_eligible is
        itself idempotent-ish per sandbox, guarded by status)."""
        d=decision.evaluate(tid)
        if d["status"]=="DONE":
            if not db.one("SELECT id FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='TASK_COMPLETED'",(tid,)):
                required=[f"{m['repo_name']}(pr={m.get('pr_number')},merge_sha={(m.get('merged_commit') or '')[:12]})" for m in d["merge_records"] if m["required"]]
                db.event("task",tid,"TASK_COMPLETED",f"all required repos merged: {', '.join(required)}")
            for sb in task_sandboxes(tid):
                if sb["status"] not in ("CLOSED","CLEANING"): sandboxes.mark_cleanup_eligible(sb["id"])
    def _reconcile_merge_record(tid,row,status,source):
        """Section 1/5/6/7/15/16: the ONE place a freshly-fetched GitHub
        PR status ever gets written into a MergeRecord -- GitHub's own
        reported state is authoritative and always wins over whatever
        was persisted before (a PR gh reports MERGED is never left
        stuck at PR_OPEN/CONFLICT). Exact merge commit SHA and GitHub's
        own merged_at are persisted, never guessed or set to "now".
        Idempotent: re-reconciling an already-MERGED record just writes
        the same GitHub-sourced facts again and never emits a second
        PR_MERGED_DETECTED (source is WORKSPACE_MANAGER_MERGE for the
        real /merge action, GITHUB_REFRESH for a passive Refresh/
        Create-PR-reuse discovering it externally)."""
        was_merged=row["merge_status"]=="MERGED"
        new_status="MERGED" if status["pr_state"] in MERGED_STATES else ("CONFLICT" if status["mergeability"]=="CONFLICTING" else "PR_OPEN")
        if new_status=="MERGED":
            # State-consistency invariant: merge_status=='MERGED' must
            # never persist with merged_commit NULL -- GitHub always
            # returns a real merge commit for an actually-merged PR, but
            # fall back to the PR's own last-known head_sha rather than
            # ever writing MERGED with no commit to point to at all.
            merged_commit=status.get("merged_commit") or status.get("head_sha") or row.get("merged_commit")
            db.execute(
                "UPDATE merge_records SET merge_status='MERGED',pr_state=?,ci_status=?,mergeability=?,merge_state_status=?,head_sha=?,merged_commit=?,merged_at=?,last_synced_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status["pr_state"],status["ci_status"],status["mergeability"],status["merge_state_status"],status["head_sha"],
                 merged_commit,status.get("merged_at") or row.get("merged_at"),row["id"]))
        else:
            db.execute(
                "UPDATE merge_records SET merge_status=?,pr_state=?,ci_status=?,mergeability=?,merge_state_status=?,head_sha=?,last_synced_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_status,status["pr_state"],status["ci_status"],status["mergeability"],status["merge_state_status"],status["head_sha"],row["id"]))
        db.event("task",tid,"PR_REFRESHED",f"repo={row['repository_id']} pr={row['pr_number']} head={status.get('head_sha')} source={source}")
        if new_status=="MERGED" and not was_merged:
            db.event("task",tid,"PR_MERGED_DETECTED",f"repo={row['repository_id']} pr={row['pr_number']} merge_sha={status.get('merged_commit')} source={source}")
        _schedule_cleanup_if_done(tid)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/create-pr")
    def create_merge_pr(tid:int,repository_id:int, _authz: None = Depends(require_role("repository", "repository_id", "MEMBER"))):
        """[Create PR] (section 3): only from the exact verified source
        (the repo's Task Integration branch when Integration is required,
        else the Builder's own branch) -- never a branch name accepted
        from the browser. Reuses an existing OPEN/MERGED PR for the same
        head/base instead of creating a duplicate (section 3)."""
        t=task_row(tid); r=repo(repository_id); d=decision.evaluate(tid)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row: raise GitSafetyError("No MergeRecord for this repository on this Task")
        if not d["ready_for_main"]: raise GitSafetyError("Task is not READY_FOR_MAIN yet")
        branch,commit=decision.effective_source_for_repo(d,repository_id)
        if not branch or not commit: raise GitSafetyError("No verified source branch/commit for this repository")
        if not github_merge.available(r["repo_path"]): raise GitSafetyError("Repository has no GitHub remote -- use Confirm External Merge instead")
        base_branch=r["default_branch"] or "main"
        try: op_id=ops.begin("merge_record",row["id"],"CREATE_PR")
        except OperationInProgress: return RedirectResponse(f"/tasks/{tid}",303)
        try:
            # Neither a Builder Workspace branch nor a Task Integration
            # branch is ever pushed anywhere automatically before this
            # point (both only exist as local worktree branches) --
            # push the exact verified branch to origin first, every
            # time, so GitHub actually has something to open a PR
            # against (and so a later commit landing on the same branch
            # gets synced too, not just the first push).
            github_merge.push_branch(r["repo_path"],branch)
            existing=github_merge.find_existing_pr(r["repo_path"],branch,base_branch)
            reused=bool(existing and existing["state"]!="CLOSED")
            if reused:
                status=github_merge.pr_status(r["repo_path"],existing["number"])
            else:
                title,body=t["title"],f"Automated PR for Task #{t['id']}: {t['title']}\n\nGenerated by ProjectFlow Workspace Manager."
                status=github_merge.create_pr(r["repo_path"],branch,base_branch,title,body)
                db.event("task",tid,"PR_CREATED",f"repo={repository_id} pr={status['pr_number']} head={status['head_sha']}")
        except GitHubIntegrationError as exc:
            ops.fail(op_id,f"{exc.code}: {exc}")
            raise GitSafetyError(f"{exc.code}: {exc}") from exc
        # pr_number/pr_url/base_branch/source_branch/verified_commit are
        # pinned here (this is the one place they're first established);
        # everything else GitHub-authoritative (merge_status/merged_commit/
        # merged_at included) goes through the same reconciliation helper
        # Refresh/Merge use, so re-discovering an ALREADY-merged PR while
        # reusing it (section 6/7) is handled identically, never a second
        # ad hoc "is it merged" check.
        db.execute(
            "UPDATE merge_records SET pr_number=?,pr_url=?,base_branch=?,source_branch=?,verified_commit=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status["pr_number"],status["pr_url"],base_branch,branch,commit,row["id"]))
        row=db.one("SELECT * FROM merge_records WHERE id=?",(row["id"],))
        _reconcile_merge_record(tid,row,status,source="GITHUB_REFRESH")
        ops.succeed(op_id,f"PR #{status['pr_number']} already open" if reused else f"PR #{status['pr_number']} created")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/refresh")
    def refresh_merge_pr(tid:int,repository_id:int, _authz: None = Depends(require_role("repository", "repository_id", "MEMBER"))):
        """[Refresh] (section 2/5/7): re-reads live PR/CI/mergeability
        from GitHub -- never trusts whatever was last rendered. If
        GitHub reports the PR as MERGED (whether Workspace Manager
        merged it or it was merged directly on GitHub), reconciles the
        MergeRecord to MERGED right here, in the same request -- no
        second button press, no stale READY_FOR_MAIN left standing
        (section 7/19)."""
        task_row(tid); r=repo(repository_id)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row or not row["pr_number"]: raise GitSafetyError("No PR to refresh yet")
        try: status=github_merge.pr_status(r["repo_path"],row["pr_number"])
        except GitHubIntegrationError as exc: raise GitSafetyError(f"{exc.code}: {exc}") from exc
        _reconcile_merge_record(tid,row,status,source="GITHUB_REFRESH")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/merge")
    def real_merge_pr(tid:int,repository_id:int, _authz: None = Depends(require_role("repository", "repository_id", "MEMBER"))):
        """[Merge] (section 5): re-fetches PR status, revalidates head
        SHA/CI/mergeability against the Task's live decision one more
        time right here (never trusts whatever the page last rendered),
        then -- only if every gate still actually passes -- calls the
        real GitHub merge API. Persists the exact merge commit GitHub
        reports back, never a guess."""
        t=task_row(tid); r=repo(repository_id)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row or not row["pr_number"]: raise GitSafetyError("No open PR for this repository")
        try: op_id=ops.begin("merge_record",row["id"],"MERGE_PR")
        except OperationInProgress: return RedirectResponse(f"/tasks/{tid}",303)
        try: status=github_merge.pr_status(r["repo_path"],row["pr_number"])
        except GitHubIntegrationError as exc:
            ops.fail(op_id,f"{exc.code}: {exc}")
            raise GitSafetyError(f"{exc.code}: {exc}") from exc
        db.execute(
            "UPDATE merge_records SET pr_state=?,ci_status=?,mergeability=?,merge_state_status=?,head_sha=?,last_synced_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status["pr_state"],status["ci_status"],status["mergeability"],status["merge_state_status"],status["head_sha"],row["id"]))
        row=db.one("SELECT * FROM merge_records WHERE id=?",(row["id"],))
        d=decision.evaluate(tid)
        gate=decision.merge_gate_status(d,repository_id,row)
        if not gate["eligible"]:
            db.event("task",tid,"MERGE_BLOCKED",f"repo={repository_id} blockers={','.join(gate['blockers'])}")
            ops.fail(op_id,f"Merge blocked: {', '.join(gate['blockers'])}")
            raise GitSafetyError(f"Merge blocked: {', '.join(gate['blockers'])}")
        db.event("task",tid,"MERGE_REQUESTED",f"repo={repository_id} pr={row['pr_number']} head={row['head_sha']}")
        try: merged=github_merge.merge_pr(r["repo_path"],row["pr_number"],row["merge_strategy"] or "MERGE_COMMIT")
        except GitHubIntegrationError as exc:
            db.event("task",tid,"MERGE_BLOCKED",f"repo={repository_id} error={exc.code}")
            ops.fail(op_id,f"{exc.code}: {exc}")
            raise GitSafetyError(f"{exc.code}: {exc}") from exc
        # Persisted through the same reconciliation helper Refresh/Create-PR
        # use (section 6): exact merge commit SHA + GitHub's own merged_at,
        # MERGE_SUCCEEDED recorded here for the WM-initiated action
        # specifically, PR_MERGED_DETECTED/TASK_COMPLETED handled by the
        # helper itself -- Task DONE is immediate, no later Refresh needed.
        db.event("task",tid,"MERGE_SUCCEEDED",f"repo={repository_id} pr={row['pr_number']} merge_sha={merged['merged_commit']}")
        _reconcile_merge_record(tid,row,merged,source="WORKSPACE_MANAGER_MERGE")
        db.execute("UPDATE merge_records SET pr_ref=? WHERE id=?",(merged["pr_url"],row["id"]))
        ops.succeed(op_id,f"Merged {(merged['merged_commit'] or '')[:8]}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/confirm-external-merge")
    def confirm_external_merge(tid:int,repository_id:int,merged_commit:str=Form(""),reason:str=Form(...), _authz: None = Depends(require_role("repository", "repository_id", "MEMBER"))):
        """Manual fallback (section 12), Advanced-only: real ancestry
        verification -- refuses unless the verified source commit is
        actually an ancestor of the target branch's real, freshly-fetched
        HEAD. Never a bare 'trust the click' (unlike the legacy
        mark-merged route this supersedes for GitHub-less repos)."""
        t=task_row(tid); r=repo(repository_id); d=decision.evaluate(tid)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row: raise GitSafetyError("No MergeRecord for this repository on this Task")
        branch,commit=decision.effective_source_for_repo(d,repository_id)
        if not commit: raise GitSafetyError("No verified source commit for this repository")
        base_branch=row["base_branch"] or r["default_branch"] or "main"
        target_head=github_merge.target_head(r["repo_path"],base_branch)
        if not target_head or not github_merge.is_ancestor(r["repo_path"],commit,f"origin/{base_branch}"):
            raise GitSafetyError(f"Verified commit {(commit or '?')[:12]} is not an ancestor of origin/{base_branch} -- cannot confirm external merge")
        final_commit=merged_commit.strip() or target_head
        db.execute(
            "UPDATE merge_records SET merge_status='MERGED',merged_commit=?,verified_commit=?,base_branch=?,external_merge_reason=?,merged_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (final_commit,commit,base_branch,reason.strip(),row["id"]))
        db.event("task",tid,"EXTERNAL_MERGE_CONFIRMED",f"repo={repository_id} commit={final_commit} reason={reason.strip()[:200]}")
        _schedule_cleanup_if_done(tid)
        return RedirectResponse(f"/tasks/{tid}",303)

    # ------------------------------------------------------------ Deployment
    # Section 23: a genuinely separate lifecycle from Task/MergeRecord --
    # never folded into TaskDecisionService. A Task stays DONE regardless
    # of what happens here; a Deployment row is never the thing that
    # decides Task status.
    DEPLOY_ENVIRONMENTS=("DEV",)
    def latest_deployment(task_id,repository_id,environment):
        return db.one("SELECT * FROM deployments WHERE task_id=? AND repository_id=? AND environment=? ORDER BY id DESC LIMIT 1",(task_id,repository_id,environment))
    def deployment_source(tid,repository_id):
        """Section 3/24: the ONLY authoritative source for a post-merge
        deployment is the exact GitHub merge commit already recorded on
        this repo's own MergeRecord -- never the integration/agent
        branch (those may have moved on, or been cleaned up, since
        merge), never a guess. Refuses if the repo isn't actually
        MERGED yet."""
        mr=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not mr or mr["merge_status"]!="MERGED" or not mr.get("merged_commit"):
            raise GitSafetyError("This repository is not MERGED yet -- nothing to deploy")
        return mr["merged_commit"], mr.get("base_branch") or "main"
    @app.post("/api/tasks/{tid}/deployments")
    def create_deployment(tid:int,repository_id:int=Form(...),environment:str=Form("DEV"), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """[Deploy to DEV]: only reachable once the repo is actually
        MERGED (section 1/25) -- never the old workflow's Merge/Create
        PR/Push/Mark Ready actions, and never re-merges or re-touches
        PR state. Duplicate-click protection (section 22): a second
        click while a deployment for this exact task/repo/environment
        is still in flight just redirects back to it, never starts a
        second real build/deploy."""
        t=task_row(tid); r=repo(repository_id)
        environment=environment.strip().upper()
        if environment not in DEPLOY_ENVIRONMENTS: raise GitSafetyError(f"Unknown environment: {environment}")
        d=decision.evaluate(tid)
        if d["status"]!="DONE": raise GitSafetyError("Task is not DONE yet -- nothing to deploy")
        target=deployer.target(r["repo_path"],environment)
        if not target: raise GitSafetyError(f"{environment} target not configured for this repository")
        existing=latest_deployment(tid,repository_id,environment)
        if existing and existing["status"] in ("PENDING","PREPARING","BUILDING","DEPLOYING","VERIFYING"):
            return RedirectResponse(f"/tasks/{tid}",303)
        source_commit,source_branch=deployment_source(tid,repository_id)
        try:
            spec_baseline=SpecRegistry(specs_root).load().baseline_digest()
        except SpecError:
            # S10: a broken spec tree never blocks a deployment -- this
            # column is a traceability pointer (what spec baseline was
            # current when this deployment was made), not a gate.
            spec_baseline=None
        did=db.execute(
            "INSERT INTO deployments(task_id,repository_id,environment,target_name,source_branch,source_commit,status,spec_baseline_sha256) VALUES(?,?,?,?,?,?,'PENDING',?)",
            (tid,repository_id,environment,target.get("target"),source_branch,source_commit,spec_baseline))
        db.event("task",tid,"DEPLOYMENT_REQUESTED",f"repo={repository_id} env={environment} commit={source_commit[:12]} deployment={did}")
        deployer.deploy(did)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/deployments/{did}/redeploy")
    def redeploy(did:int, _authz: None = Depends(require_role("deployment", "did", "ADMIN"))):
        """[Redeploy] (section 19): always the SAME exact source_commit
        as the deployment being redeployed -- never silently deploys
        whatever main/HEAD currently is. A genuinely newer merged
        source requires going back through create_deployment (a fresh
        Task/repo lookup), never this shortcut."""
        prev=db.one("SELECT * FROM deployments WHERE id=?",(did,))
        if not prev: raise HTTPException(404)
        existing=latest_deployment(prev["task_id"],prev["repository_id"],prev["environment"])
        if existing and existing["status"] in ("PENDING","PREPARING","BUILDING","DEPLOYING","VERIFYING"):
            return RedirectResponse(f"/tasks/{prev['task_id']}",303)
        did2=db.execute(
            "INSERT INTO deployments(task_id,repository_id,environment,target_name,source_branch,source_commit,status) VALUES(?,?,?,?,?,?,'PENDING')",
            (prev["task_id"],prev["repository_id"],prev["environment"],prev["target_name"],prev["source_branch"],prev["source_commit"]))
        db.event("task",prev["task_id"],"DEPLOYMENT_REQUESTED",f"repo={prev['repository_id']} env={prev['environment']} commit={prev['source_commit'][:12]} deployment={did2} redeploy_of={did}")
        deployer.deploy(did2)
        return RedirectResponse(f"/tasks/{prev['task_id']}",303)
    @app.post("/api/deployments/{did}/rollback")
    def rollback_deployment(did:int, _authz: None = Depends(require_role("deployment", "did", "ADMIN"))):
        """[Rollback] (section 5/6/7): only ever reachable from a FAILED
        or ROLLBACK_FAILED deployment with a still-eligible prior
        VERIFIED deployment -- DeploymentService.rollback() re-checks
        this itself (never trusts the button having been rendered) and
        refuses honestly rather than faking success."""
        prev=db.one("SELECT * FROM deployments WHERE id=?",(did,))
        if not prev: raise HTTPException(404)
        existing=latest_deployment(prev["task_id"],prev["repository_id"],prev["environment"])
        if existing and existing["status"] in ("PENDING","PREPARING","BUILDING","DEPLOYING","VERIFYING"):
            return RedirectResponse(f"/tasks/{prev['task_id']}",303)
        ok,error,new_id=deployer.rollback(did)
        if not ok: raise GitSafetyError(error)
        db.event("task",prev["task_id"],"ROLLBACK_REQUESTED",f"repo={prev['repository_id']} env={prev['environment']} rollback_of={did} deployment={new_id}")
        return RedirectResponse(f"/tasks/{prev['task_id']}",303)
    @app.get("/api/deployments/{did}")
    def api_deployment(did:int, _authz: None = Depends(require_read_role("deployment", "did"))):
        row=db.one("SELECT * FROM deployments WHERE id=?",(did,))
        if not row: raise HTTPException(404)
        return row
    @app.get("/deployments/{did}",response_class=HTMLResponse)
    def deployment_detail(request:Request,did:int, _authz: None = Depends(require_read_role("deployment", "did"))):
        row=db.one("SELECT d.*,r.repo_name FROM deployments d JOIN repositories r ON r.id=d.repository_id WHERE d.id=?",(did,))
        if not row: raise HTTPException(404)
        phases=db.all("SELECT * FROM deployment_phases WHERE deployment_id=? ORDER BY id",(did,))
        history=db.all("SELECT * FROM deployments WHERE task_id=? AND repository_id=? AND environment=? ORDER BY id DESC",(row["task_id"],row["repository_id"],row["environment"]))
        view=deployment_view(row,True,deployer.rollback_target(row))
        return render(request,"deployment_detail.html",d=row,phases=phases,history=history,view=view)

    @app.post("/api/tasks/{tid}/mark-merged")
    def mark_task_merged(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Bulk convenience for the common single-repo Task (or "I already
        confirmed everything is merged manually") -- marks every required
        MergeRecord MERGED at once. Task DONE still only ever falls out of
        that same MergeRecord state (section 41/42), never a status value
        this route sets directly."""
        task_row(tid)
        for m in decision.merge_records(tid):
            if m["required"] and m["merge_status"]!="MERGED":
                db.execute("UPDATE merge_records SET merge_status='MERGED',merged_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(m["id"],))
        db.event("task",tid,"ALL_MERGES_RECORDED")
        for sb in task_sandboxes(tid):
            if sb["status"] not in ("CLOSED","CLEANING"): sandboxes.mark_cleanup_eligible(sb["id"])
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/close")
    def close_task(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        """Close (section 41/42): available once computed status is DONE.
        closed_at is only ever a timestamp annotation on an already-DONE
        Task, never its own status -- and forcing sandbox cleanup here is
        independent of that: a Task can be DONE for a long time with its
        sandboxes still sitting in their normal retention window."""
        t=task_row(tid); d=decision.evaluate(tid)
        if d["status"]!="DONE": raise GitSafetyError(f"Task is not DONE yet (status={d['status']})")
        db.execute("UPDATE tasks SET closed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_CLOSED")
        for sb in task_sandboxes(tid):
            if sb["status"] not in ("CLOSED","CLEANING"): sandboxes.cleanup(sb["id"],force=True)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/cancel")
    def cancel_task(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid); db.execute("UPDATE tasks SET status='CANCELLED',closed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_CANCELLED")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/workspaces/{wid}/create-sandbox")
    def create_workspace_sandbox(tid:int,wid:int,profile:str=Form(""), _authz: None = Depends(require_role("workspace", "wid", "MEMBER"))):
        t=task_row(tid); w=agent_row(wid)
        if w["task_id"]!=tid: raise HTTPException(404,"Workspace does not belong to this task")
        sid=auto_create_sandbox(tid,w["repository_id"],w["repo_path"],"AGENT_WORKSPACE",wid,w["role"] or w["agent"],w["branch"],w["last_commit"],w["worktree_path"],profile.strip().upper() or None,t.get("default_sandbox_profile"))
        if sid: db.execute("UPDATE agent_workspaces SET sandbox_profile=(SELECT profile FROM sandboxes WHERE id=?) WHERE id=?",(sid,wid))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/extend-retention")
    def extend_task_retention(tid:int,hours:int=Form(24), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        for sb in task_sandboxes(tid):
            if sb["status"]!="CLOSED": sandboxes.mark_cleanup_eligible(sb["id"],hours)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/cleanup-now")
    def cleanup_task_now(tid:int, _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        for sb in task_sandboxes(tid):
            if sb["status"]!="CLOSED": sandboxes.cleanup(sb["id"],force=True)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/verification-report")
    def submit_task_report(tid:int,work_status:str=Form("READY"),what_changed:str=Form(""),automated_tests:str=Form(""),how_to_verify:str=Form(""),expected_result:str=Form(""),test_data:str=Form(""),runtime_requirements:str=Form("NONE"),risks:str=Form(""), _authz: None = Depends(require_role("task", "tid", "MEMBER"))):
        task_row(tid)
        db.execute("INSERT INTO verification_reports(task_id,workspace_id,work_status,what_changed,automated_tests,how_to_verify,expected_result,test_data,runtime_requirements,risks) VALUES(?,NULL,?,?,?,?,?,?,?,?)",
                   (tid,work_status.strip().upper() or "READY",what_changed.strip(),automated_tests.strip(),how_to_verify.strip(),expected_result.strip(),test_data.strip(),runtime_requirements.strip().upper() or "NONE",risks.strip()))
        db.event("task",tid,"VERIFICATION_REPORT_ADDED",work_status)
        return RedirectResponse(f"/tasks/{tid}",303)

    # ------------------------------------------------------------ Sandboxes
    @app.get("/sandboxes",response_class=HTMLResponse)
    def sandboxes_page(request:Request,status:str="",task_id:str="",repository_id:str="",owner_type:str="",profile:str=""):
        clauses=[]; params=[]
        if status=="running": clauses.append("s.status='RUNNING'")
        elif status=="unhealthy": clauses.append("s.status='RUNNING' AND s.health_status!='HEALTHY'")
        elif status=="stopped": clauses.append("s.status='STOPPED'")
        elif status=="cleanup_pending": clauses.append("s.status='CLEANUP_ELIGIBLE'")
        if task_id: clauses.append("s.task_id=?"); params.append(int(task_id))
        if repository_id: clauses.append("s.repository_id=?"); params.append(int(repository_id))
        if owner_type: clauses.append("s.owner_type=?"); params.append(owner_type)
        if profile: clauses.append("s.profile=?"); params.append(profile)
        where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
        rows=_filter_polymorphic(request,"sandbox",db.all(f"SELECT s.*,r.repo_name,t.title task_title FROM sandboxes s LEFT JOIN repositories r ON r.id=s.repository_id LEFT JOIN tasks t ON t.id=s.task_id{where} ORDER BY s.updated_at DESC",tuple(params)))
        repo_ids=_visible_repo_ids(request)
        # B5.2: this "running" badge is a capacity indicator ("N of
        # max_running slots used" -- settings.max_running_sandboxes is
        # itself a real, unfiltered, whole-process ceiling, so this
        # badge deliberately mirrors that same scope, just tenant-
        # filtered) -- independent of this page's own status/task/repo
        # filters above (rows), which must never change what this
        # number means.
        return render(request,"sandboxes.html",sandboxes=[sandbox_view(r) for r in rows],running=sandboxes.running_count(repo_ids,_visible_task_ids(request)),max_running=settings.max_running_sandboxes,
                      filters={"status":status,"task_id":task_id,"repository_id":repository_id,"owner_type":owner_type,"profile":profile},
                      tasks=_filter_rows(db.all("SELECT id,title FROM tasks ORDER BY title"),_visible_task_ids(request)),
                      repositories=_filter_rows(db.all("SELECT * FROM repositories WHERE enabled=1"),repo_ids))
    @app.get("/api/sandboxes")
    def api_sandboxes(request: Request): return _filter_polymorphic(request,"sandbox",db.all("SELECT * FROM sandboxes ORDER BY id DESC"))
    @app.get("/sandboxes/{sid}",response_class=HTMLResponse)
    def sandbox_detail(request:Request,sid:int, _authz: None = Depends(require_read_role("sandbox", "sid"))):
        sb=sandbox_row(sid); sources=db.all("SELECT s.*,r.repo_name FROM sandbox_sources s JOIN repositories r ON r.id=s.repository_id WHERE s.sandbox_id=?",(sid,))
        ports_=ports.ports_for(sid); ops=db.all("SELECT * FROM sandbox_operations WHERE sandbox_id=? ORDER BY id DESC LIMIT 20",(sid,)); hw=db.all("SELECT * FROM hardware_test_results WHERE sandbox_id=? ORDER BY id DESC",(sid,))
        outputs_=sandboxes.outputs(sid); lan_ip=sandbox_runtime.local_ip() if sb["profile"]=="HARDWARE" else None
        task=db.one("SELECT id,title FROM tasks WHERE id=?",(sb["task_id"],)) if sb["task_id"] else None
        stale=sandboxes.is_stale(sid,sandbox_current_commits(sid))
        primary=sources[0] if sources else None
        manual=manual_verification_status(sid,primary["worktree_path"]) if primary else {"status":"NOT_RUN","row":None}
        manual_history=db.all("SELECT * FROM manual_verifications WHERE sandbox_id=? ORDER BY id DESC",(sid,))
        return render(request,"sandbox_detail.html",sb=sb,sources=sources,ports=ports_,ops=ops,hw=hw,outputs=outputs_,lan_ip=lan_ip,
                      task=task,stale=stale,view=sandbox_view(sb),manual=manual,manual_history=manual_history)
    @app.get("/api/sandboxes/{sid}")
    def api_sandbox(sid:int, _authz: None = Depends(require_read_role("sandbox", "sid"))): return sandbox_row(sid)
    @app.get("/api/sandboxes/{sid}/status")
    def api_sandbox_status(sid:int, _authz: None = Depends(require_read_role("sandbox", "sid"))):
        """Small polling endpoint for the Provision/Reset Data/Cleanup
        button-feedback JS: sandbox.status + its own most recent
        sandbox_operations row (already the real source of truth --
        never a second ledger, per db.py V12's comment)."""
        sb=sandbox_row(sid)
        op=db.one("SELECT * FROM sandbox_operations WHERE sandbox_id=? ORDER BY id DESC LIMIT 1",(sid,))
        return {"status":sb["status"],"health_status":sb["health_status"],"error_code":sb["error_code"],"error_message":sb["error_message"],"latest_operation":op}
    SANDBOX_BUSY_STATUSES=("PROVISIONING","STARTING","RESETTING","CLEANING")
    @app.post("/api/sandboxes/{sid}/start")
    def start_sandbox(sid:int, _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))):
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        sandboxes.provision(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/stop")
    def stop_sandbox(sid:int, _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))): sandbox_row(sid); sandboxes.stop(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/restart")
    def restart_sandbox(sid:int, _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))):
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        sandboxes.stop(sid); sandboxes.provision(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/rebuild")
    def rebuild_sandbox(sid:int, _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))):
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        for src in db.all("SELECT * FROM sandbox_sources WHERE sandbox_id=?",(sid,)):
            db.execute("UPDATE sandbox_sources SET commit_sha=? WHERE id=?",(git.head(src["worktree_path"]),src["id"]))
        sandboxes._write_manifest(sid); db.execute("UPDATE sandboxes SET status='CREATED',health_status='UNKNOWN' WHERE id=?",(sid,)); db.event("sandbox",sid,"SANDBOX_REBUILD_REQUESTED")
        sandboxes.provision(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/reset-data")
    def reset_sandbox_data(sid:int, _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))):
        """[Reset Sandbox Data] (sections 6-9): the first-class, audited
        way to test default-credential/first-run/seed/migration behavior
        from a genuinely clean state -- never a raw docker compose down
        -v exposed to the browser. Real ownership check + volume removal
        + re-provision from the exact current source, all through
        SandboxManager, all recorded as a RESET_DATA SandboxOperation.
        Runs in a background thread (SandboxManager.reset_data) so the
        button-feedback state (RESETTING -> PROVISIONING -> RUNNING)
        survives a refresh instead of freezing the tab; a click while
        already busy is a no-op, never a second concurrent reset."""
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        sandboxes.reset_data(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/health")
    def health_sandbox(sid:int, _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))): sandbox_row(sid); sandboxes.health_check(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/cleanup")
    def cleanup_sandbox_now(sid:int, _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))):
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        sandboxes.cleanup(sid,force=True); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/extend-retention")
    def extend_retention(sid:int,hours:int=Form(24), _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))): sandbox_row(sid); sandboxes.mark_cleanup_eligible(sid,hours); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/hardware-test")
    def record_hardware_test(sid:int,result:str=Form(...),notes:str=Form(""), _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))):
        sandbox_row(sid)
        if result not in ("PASS","FAIL","NOT_RUN"): raise GitSafetyError("Invalid hardware test result")
        db.execute("INSERT INTO hardware_test_results(sandbox_id,result,notes) VALUES(?,?,?)",(sid,result,notes)); db.event("sandbox",sid,"HARDWARE_TEST_RECORDED",result)
        return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/manual-verification")
    def record_manual_verification(sid:int,result:str=Form(...),note:str=Form(""), _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))):
        """Verification guard (QA Center sandbox spec section 15): a
        sandbox that was never HEALTHY is a setup/runtime problem, not a
        QA finding -- neither PASS nor FAIL means anything recorded
        against it (status stays RUNNING only once health_check() last
        confirmed it, never a stale/optimistic flag). PASS additionally
        requires the sandbox to be HEALTHY *right now* and not stale
        (its pinned source is still the branch's real current HEAD) --
        never a PASS confirming a build that's already out of date.
        Enforced server-side, never only a disabled button client-side."""
        sb=sandbox_row(sid)
        if result not in ("PASS","FAIL"): raise GitSafetyError("Invalid manual verification result")
        if sb["status"]!="RUNNING":
            raise GitSafetyError(f"Cannot record verification: sandbox is {sb['status']}, not RUNNING -- this is a setup problem, not a QA result")
        if result=="PASS":
            if sb["health_status"]!="HEALTHY":
                raise GitSafetyError("Cannot record PASS: sandbox health check is not HEALTHY")
            if sandboxes.is_stale(sid,sandbox_current_commits(sid)):
                raise GitSafetyError("Cannot record PASS: sandbox source is stale (a participating branch moved since this sandbox was built) -- Rebuild first")
        # Section 16: pin the sandbox's OWN actual built commit, never a
        # live re-read of the worktree's current HEAD -- the branch may
        # have moved since this exact sandbox was built, and a
        # verification is only ever evidence for the commit that was
        # actually running, not whatever HEAD happens to be right now.
        primary=db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? ORDER BY id LIMIT 1",(sid,))
        source_commit=primary["commit_sha"] if primary else ""
        workspace_id=sb["owner_id"] if sb["owner_type"]=="AGENT_WORKSPACE" else None
        db.execute("INSERT INTO manual_verifications(task_id,workspace_id,sandbox_id,result,note,source_commit,operator) VALUES(?,?,?,?,?,?,?)",
                   (sb["task_id"],workspace_id,sid,result,note.strip()[:2000],source_commit,"ui"))
        db.event("sandbox",sid,"MANUAL_VERIFICATION_RECORDED",result)
        return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/build-firmware")
    def build_firmware(sid:int, _authz: None = Depends(require_role("sandbox", "sid", "MEMBER"))):
        sandbox_row(sid); provider=db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? ORDER BY id LIMIT 1",(sid,))
        contract=load_sandbox_contract(Path(provider["worktree_path"])); cmd=hardware_build_command(contract or {})
        if not cmd: raise SandboxError("HARDWARE_BUILD_NOT_DECLARED","sandbox.hardware.build_command not declared")
        backend_url=next(iter(sandboxes.outputs(sid).values()),"")
        # B0.6: the third real shell=True call site the audit flagged --
        # same sandboxed_exec chokepoint as TestRunner/GateWaiverService.
        # Direct-host path (AUTH_MODE=none) still inherits the full host
        # environment (today's exact behavior); the sandboxed path
        # (AUTH_MODE=required) gets ONLY these two explicit vars, never
        # the host's own os.environ (sandboxed_exec's own docstring).
        result=sandboxed_exec.run(cmd, Path(provider["worktree_path"]), ".", 900,
                                   env={"SANDBOX_BACKEND_URL":backend_url,"SANDBOX_ID":str(sid)})
        op_id=sandboxes._op_start(sid,"FIRMWARE_BUILD"); sandboxes._op_finish(op_id,"SUCCESS" if result.returncode==0 else "FAILED",result.returncode,result.stdout,result.stderr)
        return RedirectResponse(f"/sandboxes/{sid}",303)

    return app

app=create_app()
