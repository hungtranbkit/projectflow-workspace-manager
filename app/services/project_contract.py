from __future__ import annotations
from pathlib import Path
import yaml

from app.services.request_memo import RequestMemo

class ContractError(ValueError): pass

# Track A1 (A1.4/A1.5/A1.6) perf fix, found by profiling (not guessed --
# see scripts/benchmark_changes_list.py's own cProfile run): every reader
# in this module re-reads AND re-parses PROJECT.yaml from disk on every
# single call, with zero caching. ProductAcceptanceService.overview_status()
# alone (via _resolve_project_policy_for_change -> load_engineering_policy)
# was reloading it several times per Change -- for 100 Changes this cost
# MORE wall-clock time than every SQLite query GET /changes made
# combined. `_load_yaml` is the one shared, request-scoped-memoizable
# read+parse every function below goes through now; caching is opt-in
# (`with project_contract.memoize(): ...`, unmemoized -- exactly today's
# always-fresh-read behavior -- when no scope is open), so a route that
# writes PROJECT.yaml (the Contract Editor) is never wrapped in this and
# always sees a live read.
_memo = RequestMemo()

def memoize():
    """`with project_contract.memoize(): ...` around one read-only HTTP
    request/composition operation. See request_memo.py."""
    return _memo.scope()

def _load_yaml(repo: Path) -> dict | None:
    return _memo.get(str(repo), lambda: _load_yaml_uncached(repo))

def _load_yaml_uncached(repo: Path) -> dict | None:
    path = repo / "PROJECT.yaml"
    if not path.is_file(): return None
    return yaml.safe_load(path.read_text()) or {}

def _read(repo: Path) -> dict:
    data = _load_yaml(repo)
    if data is None: raise ContractError("Managed repository has no PROJECT.yaml")
    return data

def load_contract(repo: Path):
    data = _read(repo)
    commands = data.get("commands") or {}; required = (data.get("ci") or {}).get("required") or [x for x in ("preflight", "test") if x in commands]
    if not required: raise ContractError("PROJECT.yaml declares no required CI stages")
    result = []
    for stage in required:
        spec = commands.get(stage)
        if not isinstance(spec, dict) or not spec.get("command"): raise ContractError(f"Required stage missing command: {stage}")
        result.append((stage, str(spec["command"]), str(spec.get("working_directory", ".")), int(spec.get("timeout_seconds", 1800))))
    return result

def load_command(repo: Path, name: str):
    """One named command.<name> entry (build/local_deploy/smoke/
    local_status/...) -- same shape load_contract() already reads for the
    CI-required stages, just not restricted to that list. Returns None
    (never raises) when the command simply isn't declared, so a caller
    can render 'not configured' instead of a hard error -- deployment
    section 8's explicit requirement. Never a route/template constructing
    a shell command itself; PROJECT.yaml is always the one source."""
    try: data = _read(repo)
    except ContractError: return None
    spec = (data.get("commands") or {}).get(name)
    if not isinstance(spec, dict) or not spec.get("command"): return None
    return (str(spec["command"]), str(spec.get("working_directory", ".")), int(spec.get("timeout_seconds", 1800)))

# ProjectFlow's user-facing environment name -> the PROJECT.yaml
# `deployment.<key>` block it actually reads. DEV deliberately maps to
# the existing `local` block/`local_deploy` command family, not a
# `dev` block that doesn't exist anywhere in this workspace's real
# contracts -- `deployment.local` is already the established, audited,
# isolated-sandbox convention (see deployment_service.py's module
# docstring for why). TEST/PRODUCTION are named so they can reuse the
# ALSO-already-existing `deployment.test`/`deployment.production`
# blocks later without inventing new PROJECT.yaml vocabulary.
ENVIRONMENT_KEYS = {"DEV": "local", "TEST": "test", "PRODUCTION": "production"}

def deployment_config(repo: Path, environment: str) -> dict | None:
    """PROJECT.yaml's deployment.<key> block for one ProjectFlow
    environment name (see ENVIRONMENT_KEYS) -- the trusted target/
    environment registry sections 8/11 require. Returns None when the
    environment has no declared block at all, or isn't a known
    ProjectFlow environment (never guessed/defaulted)."""
    key = ENVIRONMENT_KEYS.get(environment.upper())
    if not key: return None
    try: data = _read(repo)
    except ContractError: return None
    block = (data.get("deployment") or {}).get(key)
    return block if isinstance(block, dict) else None


def load_engineering_policy(repo: Path) -> dict | None:
    """Read PROJECT.yaml's optional `engineering:` block -- a
    repository-level NARROWING of the global Role & Capability Catalog
    (app/services/engineering_catalog.py, E2 section 18) and, since
    Phase E3, of workflow profile selection
    (app/services/workflow_engine.py, E3 section 12):

      engineering:
        roles:
          BUILDER:
            allowed_providers: [codex, claude]
        workflow:
          default_profile: AGENTIC_STANDARD
          allowed_profiles: [AGENTIC_STANDARD, CONTROLLED]
        architecture:
          require_for: [CONTROLLED]
        design:
          ui_ux_when_user_facing: true
        autonomous_execution:
          enabled: true
          max_concurrent_builders: 1
          auto_start_ready_tasks: true

    Returns None if the project declares no such block at all -- a
    project without one uses safe global defaults, the same convention
    load_sandbox_contract/deployment_config already use. Never a cloned
    copy of either catalog: this is only ever consulted as an override a
    caller ANDs together with the global catalog's own result. It can
    restrict a role/provider pair or a workflow profile choice the
    global catalogs already support -- never expand beyond what they
    support, and never touch a chosen profile's own stage/gate
    requirements (there is no mechanism here that could weaken a
    CONTROLLED profile's mandatory gates; only which profile may be
    SELECTED is narrowable).

    Phase E6.19 additions, same narrow-only discipline: `architecture.
    require_for` is a list of WorkflowProfile keys that should treat the
    normally-OPTIONAL ARCHITECTURE stage as REQUIRED (WorkflowService/
    ArchitectureDesignLifecycleService consult this; PROFILE_STAGES
    itself is never mutated) -- it can only ADD a requirement, never
    remove CONTROLLED's own mandatory DESIGN stage. `design.
    ui_ux_when_user_facing` is an explicit override for UI/UX
    applicability detection (E6.10): true/false forces the decision;
    omitted leaves UiUxApplicabilityService's own structured-evidence
    heuristic in charge.

    Phase E8.1 addition: `autonomous_execution` controls whether
    AutonomousExecutionService may ever launch a Builder for this
    project's Changes at all. Absent entirely -> disabled (E8.27's own
    safety requirement: autonomous execution is opt-in, never on by
    default for a repository that never declared it). `enabled: false`
    always wins regardless of anything else. `max_concurrent_builders`
    defaults to 1 when the block exists but omits it -- this
    installation never runs more than one autonomous Builder at a time
    in E8 (E13 will handle real concurrency). `auto_start_ready_tasks`
    is a secondary switch a caller may use to pause automatic task
    SELECTION while still allowing an already-launched Builder to keep
    running; defaults to true when the block exists."""
    data = _load_yaml(repo)
    if data is None: return None
    block = data.get("engineering")
    if not block: return None
    if not isinstance(block, dict): raise ContractError("engineering: must be a mapping")
    roles = block.get("roles") or {}
    if not isinstance(roles, dict): raise ContractError("engineering.roles must be a mapping")
    for role_key, cfg in roles.items():
        if not isinstance(cfg, dict): raise ContractError(f"engineering.roles.{role_key} must be a mapping")
        allowed = cfg.get("allowed_providers")
        if allowed is not None and not isinstance(allowed, list):
            raise ContractError(f"engineering.roles.{role_key}.allowed_providers must be a list")
    workflow = block.get("workflow")
    if workflow is not None:
        if not isinstance(workflow, dict): raise ContractError("engineering.workflow must be a mapping")
        allowed_profiles = workflow.get("allowed_profiles")
        if allowed_profiles is not None and not isinstance(allowed_profiles, list):
            raise ContractError("engineering.workflow.allowed_profiles must be a list")
        default_profile = workflow.get("default_profile")
        if default_profile is not None and not isinstance(default_profile, str):
            raise ContractError("engineering.workflow.default_profile must be a string")
    architecture = block.get("architecture")
    if architecture is not None:
        if not isinstance(architecture, dict): raise ContractError("engineering.architecture must be a mapping")
        require_for = architecture.get("require_for")
        if require_for is not None and not isinstance(require_for, list):
            raise ContractError("engineering.architecture.require_for must be a list")
    design = block.get("design")
    if design is not None:
        if not isinstance(design, dict): raise ContractError("engineering.design must be a mapping")
        ui_ux = design.get("ui_ux_when_user_facing")
        if ui_ux is not None and not isinstance(ui_ux, bool):
            raise ContractError("engineering.design.ui_ux_when_user_facing must be a boolean")
    autonomous = block.get("autonomous_execution")
    if autonomous is not None:
        if not isinstance(autonomous, dict): raise ContractError("engineering.autonomous_execution must be a mapping")
        enabled = autonomous.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ContractError("engineering.autonomous_execution.enabled must be a boolean")
        max_conc = autonomous.get("max_concurrent_builders")
        if max_conc is not None and (not isinstance(max_conc, int) or isinstance(max_conc, bool) or max_conc < 1):
            raise ContractError("engineering.autonomous_execution.max_concurrent_builders must be a positive integer")
        auto_start = autonomous.get("auto_start_ready_tasks")
        if auto_start is not None and not isinstance(auto_start, bool):
            raise ContractError("engineering.autonomous_execution.auto_start_ready_tasks must be a boolean")
    return block
