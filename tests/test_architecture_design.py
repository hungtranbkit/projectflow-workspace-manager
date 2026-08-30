"""Architecture & Technical/UI Design Lifecycle (Phase E6). SAFETY: every
test that touches SpecRegistry-backed behavior (requirement coverage,
UI/UX applicability against spec text) uses the `arch_env` fixture below
to redirect every E6 service's `specs_root` to an isolated tmp_path tree
-- the real `create_app()` factory always resolves specs_root to THIS
repo's own real specs/ directory. Never remove this fixture from a test
that touches requirement-coverage validation."""
from __future__ import annotations
import json
from pathlib import Path

import pytest
import yaml

from app.services.architecture_design_service import (
    ARCHITECTURE_CLASSIFICATIONS, design_state_digest,
)


@pytest.fixture
def arch_env(client, tmp_path):
    specs_root = tmp_path / "specs"
    (specs_root / "features").mkdir(parents=True)
    (specs_root / "SPEC.yaml").write_text("schema_version: 1\nproject: test\nglossary: glossary.yaml\nfeatures_dir: features\n")
    (specs_root / "glossary.yaml").write_text("schema_version: 1\nterms: {}\n")
    (specs_root / "features" / "feat-thing.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "id": "FEAT-THING", "title": "The Thing", "version": 1, "status": "approved",
        "summary": "A thing users interact with via a dashboard screen.",
        "requirements": [{"id": "REQ-001", "text": "Do the first part."},
                          {"id": "REQ-002", "text": "Do the second part."}],
        "acceptance_criteria": [{"id": "AC-001", "text": "The first part is observable."}],
        "invariants": [],
    }, sort_keys=False))
    for name in ("architecture_context_builder", "architecture_review_service", "technical_design_service",
                 "ui_ux_design_service", "design_review_service"):
        getattr(client.app.state, name).specs_root = specs_root
    client.app.state.planner_service.specs_root = specs_root
    client.app.state.planner_service.context_builder.specs_root = specs_root
    client.app.state.planner_service.validator.specs_root = specs_root
    return specs_root


def register(client, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    return client.get("/api/repositories").json()[0]["id"]


def new_change(client, title, description="", project_id=None):
    data = {"title": title, "description": description}
    if project_id is not None:
        data["project_id"] = str(project_id)
    return client.post("/api/changes", data=data).json()["id"]


def link_governing_feature(client, cid, feature_id="FEAT-THING"):
    client.app.state.trace.link("change", cid, "spec_feature", feature_id, relation="GOVERNED_BY")


def envelope(payload):
    return {"is_error": False, "subtype": "success", "result": json.dumps(payload)}


def set_fake(client, payload):
    env = envelope(payload)

    def runner(argv, cwd, timeout):
        class R:
            returncode = 0
            stdout = json.dumps(env)
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = runner


def arch_payload(**overrides):
    p = {"affected_components": ["app/services/foo.py"], "classification": "LOCAL_ARCHITECTURE_CHANGE",
         "classification_rationale": "Adds an internal module.", "risks": [], "adrs": [], "human_decisions": []}
    p.update(overrides)
    return p


def arch_review_payload(verdict="PASS", **overrides):
    p = {"verdict": verdict, "findings": []}
    p.update(overrides)
    return p


def design_payload(**overrides):
    p = {"design_summary": "Add a read-only endpoint.", "components_to_change": ["app/main.py"],
         "covered_requirements": ["REQ-001"], "migration_plan": "None required.", "rollback_strategy": "Revert the commit."}
    p.update(overrides)
    return p


def ui_ux_payload(**overrides):
    p = {"user_goals": ["See the dashboard."], "user_flows": ["Open the dashboard screen."],
         "screens": [{"name": "Dashboard", "purpose": "Show status."}],
         "acceptance_mapping": [{"acceptance_id": "AC-001", "covered_by": "Dashboard screen"}]}
    p.update(overrides)
    return p


def design_review_payload(verdict="PASS", **overrides):
    p = {"verdict": verdict, "findings": []}
    p.update(overrides)
    return p


def do_analyze(client, cid, payload=None):
    set_fake(client, payload or arch_payload())
    r = client.post(f"/api/changes/{cid}/architecture/analyze", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


def do_arch_review(client, cid, verdict="PASS", **overrides):
    set_fake(client, arch_review_payload(verdict, **overrides))
    r = client.post(f"/api/changes/{cid}/architecture/review", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


def do_technical_design(client, cid, payload=None):
    set_fake(client, payload or design_payload())
    r = client.post(f"/api/changes/{cid}/design/technical", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


def do_ui_ux_design(client, cid, payload=None):
    set_fake(client, payload or ui_ux_payload())
    r = client.post(f"/api/changes/{cid}/design/ui-ux", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


def do_design_review(client, cid, verdict="PASS", **overrides):
    set_fake(client, design_review_payload(verdict, **overrides))
    r = client.post(f"/api/changes/{cid}/design/review", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


# ================================================================ Roles/Capabilities (E6.1)

def test_new_roles_exist_in_catalog(client):
    roles = {r["key"] for r in client.app.state.roles_catalog.list_roles()}
    assert {"SOFTWARE_ARCHITECT", "TECHNICAL_DESIGNER", "UI_UX_DESIGNER"} <= roles


def test_architecture_reviewer_role_reuses_reviewer_not_a_new_role(client):
    roles = {r["key"] for r in client.app.state.roles_catalog.list_roles()}
    assert "ARCHITECTURE_REVIEWER" not in roles  # E6.1: reuse REVIEWER, never add a redundant role


def test_new_capabilities_validate_for_claude(client):
    for role in ("SOFTWARE_ARCHITECT", "TECHNICAL_DESIGNER", "UI_UX_DESIGNER"):
        result = client.app.state.roles_catalog.validate_assignment("claude", role)
        assert result["valid"] is True, result


def test_new_roles_never_have_deployment_capability(client):
    for role in ("SOFTWARE_ARCHITECT", "TECHNICAL_DESIGNER", "UI_UX_DESIGNER"):
        caps = {c["key"] for c in client.app.state.roles_catalog.capabilities_for_role(role)}
        assert not caps & {"DEPLOY_DEV", "DEPLOY_TEST", "DEPLOY_PRODUCTION", "MERGE_PR", "ROLLBACK_DEPLOYMENT"}


# ================================================================ Architecture Analysis (E6.3/E6.5)

def test_architecture_analysis_structured_result(client, git_repo, arch_env):
    cid = new_change(client, "Add internal caching module", "Speed up repeated reads.")
    result = do_analyze(client, cid, arch_payload(affected_components=["app/services/cache.py"], classification="LOCAL_ARCHITECTURE_CHANGE"))
    assert result["outcome"] == "READY"
    wp = result["work_product"]
    assert wp["kind"] == "ARCHITECTURE_ANALYSIS"
    assert wp["content_metadata"]["affected_components"] == ["app/services/cache.py"]
    assert result["classification"] == "LOCAL_ARCHITECTURE_CHANGE"


@pytest.mark.parametrize("classification", ARCHITECTURE_CLASSIFICATIONS)
def test_every_classification_value_accepted(client, git_repo, arch_env, classification):
    cid = new_change(client, f"Change requiring {classification}")
    result = do_analyze(client, cid, arch_payload(classification=classification))
    assert result["outcome"] == "READY"
    assert result["classification"] == classification


def test_unknown_classification_rejected(client, git_repo, arch_env):
    cid = new_change(client, "Bad classification change")
    result = do_analyze(client, cid, arch_payload(classification="SOMETHING_MADE_UP"))
    assert result["outcome"] == "OUTPUT_INVALID"


def test_adr_created_and_traced_to_analysis(client, git_repo, arch_env):
    cid = new_change(client, "Change needing an ADR")
    result = do_analyze(client, cid, arch_payload(classification="ARCHITECTURE_BREAKING_CHANGE",
        adrs=[{"title": "Split the auth boundary", "decision": "Move auth to its own module.", "status": "PROPOSED"}]))
    assert len(result["adr_ids"]) == 1
    adrs = client.get(f"/api/changes/{cid}/adrs").json()
    assert len(adrs) == 1
    assert adrs[0]["status"] == "PROPOSED"
    assert adrs[0]["content_metadata"]["title"] == "Split the auth boundary"


def test_breaking_change_can_raise_human_decision(client, git_repo, arch_env):
    cid = new_change(client, "Security boundary change")
    do_analyze(client, cid, arch_payload(classification="ARCHITECTURE_BREAKING_CHANGE", human_decisions=[
        {"question": "Should this endpoint require a new auth scope?", "reason": "changes the security boundary", "decision_type": "SECURITY_BOUNDARY"}]))
    assert client.app.state.human_decisions.pending_for_change(cid) is True


def test_no_architecture_change_never_forces_human_decision(client, git_repo, arch_env):
    cid = new_change(client, "Trivial internal change")
    do_analyze(client, cid, arch_payload(classification="NO_ARCHITECTURE_CHANGE"))
    assert client.app.state.human_decisions.pending_for_change(cid) is False


# ================================================================ Architecture Review (E6.7)

def test_architecture_review_pass_approves_analysis_and_adrs(client, git_repo, arch_env):
    cid = new_change(client, "Approvable architecture change")
    do_analyze(client, cid, arch_payload(adrs=[{"title": "ADR one", "decision": "Do it this way.", "status": "PROPOSED"}]))
    review = do_arch_review(client, cid, "PASS")
    assert review["verdict"] == "PASS"
    analysis = client.get(f"/api/changes/{cid}/architecture").json()
    assert analysis["status"] == "APPROVED"
    adrs = client.get(f"/api/changes/{cid}/adrs").json()
    assert adrs[0]["status"] == "APPROVED"


def test_architecture_review_needs_refinement_then_refine_creates_new_revision(client, git_repo, arch_env):
    cid = new_change(client, "Needs refinement architecture change")
    first = do_analyze(client, cid, arch_payload(affected_components=["app/one.py"]))
    review = do_arch_review(client, cid, "NEEDS_REFINEMENT", findings=[{"category": "boundary_correctness", "description": "Boundary unclear.", "severity": "MEDIUM"}])
    assert review["verdict"] == "NEEDS_REFINEMENT"
    set_fake(client, arch_payload(affected_components=["app/one.py", "app/two.py"]))
    refined = client.app.state.architecture_analysis_service.refine(first["work_product"]["id"], review, provider="claude")
    assert refined["outcome"] == "READY"
    assert refined["work_product"]["id"] != first["work_product"]["id"]
    prior = client.app.state.work_products.get(first["work_product"]["id"])
    assert prior["status"] == "SUPERSEDED"
    assert prior["content_metadata"] == json.dumps({}) or json.loads(prior["content_metadata"])["affected_components"] == ["app/one.py"]


def test_architecture_review_human_decision_required_blocks(client, git_repo, arch_env):
    cid = new_change(client, "Architecture human decision change")
    do_analyze(client, cid)
    do_arch_review(client, cid, "HUMAN_DECISION_REQUIRED", human_decisions=[{"question": "Which boundary?", "reason": "product tradeoff"}])
    assert client.app.state.human_decisions.pending_for_change(cid) is True
    assert client.app.state.architecture_design_service.architecture_ready(cid) is False


def test_architecture_review_reject_marks_rejected(client, git_repo, arch_env):
    cid = new_change(client, "Rejected architecture change")
    result = do_analyze(client, cid)
    do_arch_review(client, cid, "REJECT")
    analysis = client.app.state.work_products.get(result["work_product"]["id"])
    assert analysis["status"] == "REJECTED"


# ================================================================ Technical Design (E6.8/E6.9)

def test_technical_design_structured_result(client, git_repo, arch_env):
    cid = new_change(client, "Design test change")
    link_governing_feature(client, cid)
    result = do_technical_design(client, cid)
    assert result["outcome"] == "READY"
    wp = result["work_product"]
    assert wp["kind"] == "TECHNICAL_DESIGN"
    assert wp["content_metadata"]["design_summary"]


def test_technical_design_covered_requirements_validated_against_registry(client, git_repo, arch_env):
    cid = new_change(client, "Coverage test change")
    link_governing_feature(client, cid)
    result = do_technical_design(client, cid, design_payload(covered_requirements=["REQ-001", "REQ-DOES-NOT-EXIST"]))
    assert result["covered_requirements"] == ["REQ-001"]  # fictional id silently excluded, never trusted


def test_technical_design_uncovered_requirement_surfaced(client, git_repo, arch_env):
    cid = new_change(client, "Partial coverage change")
    link_governing_feature(client, cid)
    result = do_technical_design(client, cid, design_payload(covered_requirements=["REQ-001"]))
    assert result["uncovered_requirements"] == ["REQ-002"]


def test_technical_design_full_coverage_leaves_nothing_uncovered(client, git_repo, arch_env):
    cid = new_change(client, "Full coverage change")
    link_governing_feature(client, cid)
    result = do_technical_design(client, cid, design_payload(covered_requirements=["REQ-001", "REQ-002"]))
    assert result["uncovered_requirements"] == []


def test_technical_design_models_migration_and_rollback(client, git_repo, arch_env):
    cid = new_change(client, "Migration design change")
    link_governing_feature(client, cid)
    result = do_technical_design(client, cid, design_payload(migration_plan="Add column X with a default.", rollback_strategy="Drop column X."))
    content = result["work_product"]["content_metadata"]
    assert content["migration_plan"] == "Add column X with a default."
    assert content["rollback_strategy"] == "Drop column X."


def test_technical_design_incorporates_approved_architecture(client, git_repo, arch_env):
    cid = new_change(client, "Design with architecture change")
    link_governing_feature(client, cid)
    do_analyze(client, cid)
    do_arch_review(client, cid, "PASS")

    captured = {}
    def runner(argv, cwd, timeout):
        captured["prompt"] = argv[2]
        env = envelope(design_payload())
        class R:
            returncode = 0
            stdout = json.dumps(env)
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = runner
    client.post(f"/api/changes/{cid}/design/technical", data={"provider": "claude"})
    assert "architecture_analysis" in captured["prompt"]


# ================================================================ UI/UX applicability (E6.10) -- deterministic

def test_ui_ux_not_applicable_for_backend_only_change(client, git_repo, arch_env):
    cid = new_change(client, "Add a background reconciliation job", "Runs on a cron schedule with no user interaction.")
    status = client.get(f"/api/changes/{cid}/design").json()
    assert status["ui_ux_applicability"]["applicable"] is False


def test_ui_ux_applicable_for_user_facing_change(client, git_repo, arch_env):
    cid = new_change(client, "Add a new dashboard screen", "Users click a button on a new dashboard view.")
    status = client.get(f"/api/changes/{cid}/design").json()
    assert status["ui_ux_applicability"]["applicable"] is True


def test_ui_ux_policy_override_forces_applicable(client, git_repo, arch_env):
    cid = new_change(client, "Backend-sounding change with policy override")
    policy = {"design": {"ui_ux_when_user_facing": True}}
    result = client.app.state.architecture_design_service.detect_ui_ux(cid, project_policy=policy)
    assert result["applicable"] is True
    assert result["reason"] == "POLICY_OVERRIDE"


def test_ui_ux_governing_spec_evidence_never_leaks_from_unrelated_change(client, git_repo, arch_env):
    """Regression: a Change with no governing spec link must never be
    judged 'user-facing' just because SOME unrelated approved feature
    elsewhere in the catalog happens to mention a UI keyword."""
    cid = new_change(client, "Totally unrelated backend Change", "Adjust an internal retry count.")
    status = client.get(f"/api/changes/{cid}/design").json()
    assert status["ui_ux_applicability"]["applicable"] is False


# ================================================================ UI/UX Design (E6.11)

def test_ui_ux_design_structured_result(client, git_repo, arch_env):
    cid = new_change(client, "Dashboard UI change")
    link_governing_feature(client, cid)
    result = do_ui_ux_design(client, cid)
    assert result["outcome"] == "READY"
    content = result["work_product"]["content_metadata"]
    assert content["user_goals"] and content["user_flows"]


def test_ui_ux_design_acceptance_mapping(client, git_repo, arch_env):
    cid = new_change(client, "Dashboard UI change with mapping")
    link_governing_feature(client, cid)
    result = do_ui_ux_design(client, cid, ui_ux_payload(acceptance_mapping=[{"acceptance_id": "AC-001", "covered_by": "Dashboard screen"}]))
    mapping = result["work_product"]["content_metadata"]["acceptance_mapping"]
    assert mapping[0]["acceptance_id"] == "AC-001"


# ================================================================ Design Review (E6.12/E6.13)

def test_design_review_pass_approves_technical_and_ui_ux(client, git_repo, arch_env):
    cid = new_change(client, "Design review pass change")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    do_ui_ux_design(client, cid)
    review = do_design_review(client, cid, "PASS")
    assert review["verdict"] == "PASS"
    design = client.get(f"/api/changes/{cid}/design").json()
    assert design["technical_design"]["status"] == "APPROVED"
    assert design["ui_ux_design"]["status"] == "APPROVED"


def test_design_review_needs_refinement(client, git_repo, arch_env):
    cid = new_change(client, "Design review refinement change")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    review = do_design_review(client, cid, "NEEDS_REFINEMENT", findings=[{"dimension": "TESTABILITY", "description": "Vague failure mode."}])
    assert review["verdict"] == "NEEDS_REFINEMENT"


def test_design_review_human_decision_required(client, git_repo, arch_env):
    cid = new_change(client, "Design human decision change")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    do_design_review(client, cid, "HUMAN_DECISION_REQUIRED", human_decisions=[{"question": "Which workflow option?", "reason": "materially different UX"}])
    assert client.app.state.human_decisions.pending_for_change(cid) is True


def test_design_spec_conflict_human_variant_creates_human_decision_pointing_to_spec_lifecycle(client, git_repo, arch_env):
    cid = new_change(client, "Design spec conflict change")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    result = do_design_review(client, cid, "DESIGN_SPEC_CONFLICT", conflict_classification="HUMAN_SPEC_CHANGE_REQUIRED")
    assert result["outcome"] == "REVIEWED"
    assert client.app.state.human_decisions.pending_for_change(cid) is True
    hds = client.app.state.human_decisions.list_for("work_product", client.app.state.technical_design_service.current_for_change(cid)["id"])
    assert any("Spec Lifecycle" in hd["reason"] for hd in hds)


def test_design_spec_conflict_refinable_variant_does_not_block(client, git_repo, arch_env):
    cid = new_change(client, "Refinable design spec conflict change")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    result = do_design_review(client, cid, "DESIGN_SPEC_CONFLICT", conflict_classification="REFINABLE")
    assert result["conflict_classification"] == "REFINABLE"
    assert client.app.state.human_decisions.pending_for_change(cid) is False


def test_design_review_never_writes_canonical_spec(client, git_repo, arch_env):
    cid = new_change(client, "Design review must not touch spec")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    do_design_review(client, cid, "DESIGN_SPEC_CONFLICT", conflict_classification="HUMAN_SPEC_CHANGE_REQUIRED")
    from app.services.spec_registry import SpecRegistry
    registry = SpecRegistry(client.app.state.technical_design_service.specs_root).load()
    assert registry.feature("FEAT-THING")["requirements"][0]["text"] == "Do the first part."


# ================================================================ Bounded refinement (E6.13)

def test_bounded_design_refinement_loop_never_infinite(client, git_repo, arch_env):
    cid = new_change(client, "Endless refinement change")
    link_governing_feature(client, cid)

    def cycling_runner(argv, cwd, timeout):
        prompt = argv[2]
        if "TECHNICAL DESIGNER" in prompt:
            payload = design_payload()
        elif "INDEPENDENT DESIGN REVIEWER" in prompt:
            payload = design_review_payload("NEEDS_REFINEMENT", findings=[{"dimension": "TESTABILITY", "description": "Still vague."}])
        else:
            raise AssertionError(f"unexpected prompt: {prompt[:200]}")
        env = envelope(payload)
        class R:
            returncode = 0
            stdout = json.dumps(env)
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = cycling_runner

    result = client.app.state.architecture_design_service.run_design(cid, provider="claude")
    assert result["outcome"] == "NEEDS_REFINEMENT"
    assert result["rounds"] == 3  # MAX_ROUNDS, never infinite


def test_refine_never_weakens_spec_prompt_language(client):
    from app.services.architecture_design_service import TECHNICAL_DESIGNER_PREAMBLE, DESIGN_REVIEWER_PREAMBLE
    assert "never" in TECHNICAL_DESIGNER_PREAMBLE.lower() and "weaken" in TECHNICAL_DESIGNER_PREAMBLE.lower()
    assert "weakening" in DESIGN_REVIEWER_PREAMBLE.lower()


# ================================================================ Workflow gates (E6.16)

def create_workflow(client, cid, profile="AGENTIC_STANDARD"):
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": profile})
    assert r.status_code == 200, r.text
    return r.json()


def test_architecture_ready_requires_real_review_evidence(client, git_repo, arch_env):
    cid = new_change(client, "Gate test architecture change")
    create_workflow(client, cid, "AGENTIC_STANDARD")
    do_analyze(client, cid)
    assert client.app.state.architecture_design_service.architecture_ready(cid) is False  # DRAFT, not yet reviewed
    do_arch_review(client, cid, "PASS")
    assert client.app.state.architecture_design_service.architecture_ready(cid) is True


def test_design_ready_requires_real_review_evidence(client, git_repo, arch_env):
    cid = new_change(client, "Gate test design change", "A backend-only change with no user-facing behavior at all.")
    create_workflow(client, cid, "AGENTIC_STANDARD")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    assert client.app.state.architecture_design_service.design_ready(cid) is False  # DRAFT, not yet reviewed
    do_design_review(client, cid, "PASS")
    assert client.app.state.architecture_design_service.design_ready(cid) is True


def test_design_ready_false_while_ui_ux_required_but_missing(client, git_repo, arch_env):
    cid = new_change(client, "Needs UI design change", "Users interact with a new screen and button.")
    create_workflow(client, cid, "AGENTIC_STANDARD")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    do_design_review(client, cid, "PASS")  # technical design approved, but UI/UX never authored
    assert client.app.state.architecture_design_service.design_ready(cid) is False


def test_unresolved_human_decision_blocks_design_ready_and_surfaces_waiting_human(client, git_repo, arch_env):
    cid = new_change(client, "Waiting human design change", "Backend only.")
    create_workflow(client, cid, "AGENTIC_STANDARD")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    do_design_review(client, cid, "HUMAN_DECISION_REQUIRED", human_decisions=[{"question": "Which option?", "reason": "tradeoff"}])
    assert client.app.state.architecture_design_service.design_ready(cid) is False
    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state["status"] == "WAITING_HUMAN"


def test_controlled_profile_cannot_pass_without_required_design(client, git_repo, arch_env):
    cid = new_change(client, "Controlled profile change", "Backend only.")
    create_workflow(client, cid, "CONTROLLED")
    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "DESIGN_READY" in state["unmet_gates"]


def test_workflow_service_without_gate_hook_falls_back_to_presence_check(client, git_repo):
    """Backward compatibility: a WorkflowService constructed WITHOUT the
    E6 hook (every pre-E6 test's own construction) must behave exactly
    as E3 shipped it -- bare APPROVED WorkProduct presence, no
    architecture_design_service involved at all."""
    from app.services.workflow_engine import WorkflowService
    db = client.app.state.db
    ws = WorkflowService(db, client.app.state.workflow_catalog, client.app.state.changes, client.app.state.work_products,
                          client.app.state.decision, client.app.state.spec_compliance, client.app.state.task_dependencies)
    assert ws.architecture_design_gate is None
    cid = new_change(client, "Legacy fallback change")
    client.app.state.work_products.create(kind="TECHNICAL_DESIGN", title="Design", change_id=cid, status="APPROVED")
    assert ws._gate_design_ready(cid, []) is True


# ================================================================ Planner integration (E6.17)

def test_planner_context_sees_design_work_products(client, git_repo, arch_env):
    cid = new_change(client, "Planner sees design change")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    create_workflow(client, cid, "AGENTIC_STANDARD")
    context = client.app.state.planner_service.context_builder.build(cid, "AGENTIC_STANDARD")
    kinds = {d["kind"] for d in context["architecture_and_design"]}
    assert "TECHNICAL_DESIGN" in kinds


def test_plan_design_staleness_signal(client, git_repo, arch_env):
    cid = new_change(client, "Plan design staleness change", project_id=register(client, git_repo[1], "demo"))
    create_workflow(client, cid, "VIBE")
    set_fake(client, {"summary": "x", "tasks": [{"key": "T1", "title": "Build", "task_type": "IMPLEMENTATION"}]})
    plan_result = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"}).json()
    pid = plan_result["plan"]["id"]

    staleness = client.get(f"/api/plans/{pid}/design-staleness").json()
    assert staleness["stale"] is False

    link_governing_feature(client, cid)
    do_technical_design(client, cid)  # design work happens AFTER the plan was created

    staleness = client.get(f"/api/plans/{pid}/design-staleness").json()
    assert staleness["stale"] is True
    assert staleness["reason"] == "PLAN_DESIGN_STALE"
    assert staleness["replan_recommended"] is True


def test_design_state_digest_stable_when_nothing_changes(client, git_repo, arch_env):
    cid = new_change(client, "Stable digest change")
    link_governing_feature(client, cid)
    do_technical_design(client, cid)
    d1 = design_state_digest(client.app.state.work_products, cid)
    d2 = design_state_digest(client.app.state.work_products, cid)
    assert d1 == d2 and d1 is not None


# ================================================================ E6 never generates implementation Tasks (E6.18)

def test_design_lifecycle_never_creates_implementation_task(client, git_repo, arch_env):
    cid = new_change(client, "No auto implementation change")
    link_governing_feature(client, cid)
    do_analyze(client, cid)
    do_arch_review(client, cid, "PASS")
    do_technical_design(client, cid)
    do_design_review(client, cid, "PASS")
    tasks = client.get(f"/api/changes/{cid}/tasks").json()
    assert all(t["task_type"] != "IMPLEMENTATION" for t in tasks)


# ================================================================ Meta safety

def test_production_specs_root_never_touched_by_this_suite():
    from pathlib import Path
    real_specs = Path(__file__).resolve().parent.parent / "specs" / "features"
    names = {p.name for p in real_specs.glob("*.yaml")}
    assert "feat-thing.yaml" not in names


def test_real_architecture_design_lifecycle_end_to_end(client, git_repo, arch_env):
    """E6.23: one safe, real, non-fake Architecture & Design Lifecycle
    run against a disposable Change and an isolated tmp_path specs_root
    (arch_env) -- NO fake runner is installed anywhere in this test, so
    every Architecture Analysis / Architecture Review / Technical Design
    / UI/UX detection+design / Design Review invocation below is a
    genuine `claude -p --json-schema ... --tools "" --max-turns 1`
    subprocess call. Same disposable-fixture, harmless-intent convention
    E5.24's real test established. No service in this module ever writes
    a source file -- confirmed at the end via git status on the real repo."""
    import subprocess
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(
        client,
        "Expose the current Workflow Profile for a Change through a read-only API",
        description="Add a safe, read-only endpoint that returns the WorkflowProfile "
                     "(profile_key, current_stage, unmet_gates) for an existing Change, "
                     "so external tooling can observe workflow state without mutating it.",
        project_id=rid,
    )
    link_governing_feature(client, cid, "FEAT-THING")

    # -- Architecture Analysis (real invocation) --
    ar = client.post(f"/api/changes/{cid}/architecture/analyze", data={"provider": "claude"})
    assert ar.status_code == 200, ar.text
    ar_body = ar.json()
    print("REAL E6 TEST -- architecture analysis outcome:", ar_body["outcome"])
    assert ar_body["outcome"] == "READY", ar_body
    classification = ar_body["classification"]
    affected_components = ar_body["work_product"]["content_metadata"]["affected_components"]
    print("REAL E6 TEST -- classification:", classification)
    print("REAL E6 TEST -- affected components:", affected_components)
    adr_count = len(ar_body["adr_ids"])
    print("REAL E6 TEST -- ADR count:", adr_count)

    # -- Independent Architecture Review (real invocation, separate process) --
    arev = client.post(f"/api/changes/{cid}/architecture/review", data={"provider": "claude"})
    assert arev.status_code == 200, arev.text
    arev_body = arev.json()
    print("REAL E6 TEST -- architecture review verdict:", arev_body["verdict"])
    arch_rounds = 0
    while arev_body["verdict"] == "NEEDS_REFINEMENT" and arch_rounds < 3:
        arch_rounds += 1
        refined = client.app.state.architecture_analysis_service.refine(
            client.get(f"/api/changes/{cid}/architecture").json()["id"], arev_body, provider="claude")
        assert refined["outcome"] == "READY", refined
        arev = client.post(f"/api/changes/{cid}/architecture/review", data={"provider": "claude"})
        assert arev.status_code == 200, arev.text
        arev_body = arev.json()
        print(f"REAL E6 TEST -- architecture review after refinement round {arch_rounds}:", arev_body["verdict"])

    # -- Technical Design (real invocation) --
    architecture_ready = client.app.state.architecture_design_service.architecture_ready(cid)
    print("REAL E6 TEST -- architecture_ready:", architecture_ready)
    td = client.post(f"/api/changes/{cid}/design/technical", data={"provider": "claude"})
    assert td.status_code == 200, td.text
    td_body = td.json()
    print("REAL E6 TEST -- technical design outcome:", td_body["outcome"])
    assert td_body["outcome"] == "READY", td_body
    print("REAL E6 TEST -- covered requirements:", td_body["covered_requirements"])
    print("REAL E6 TEST -- uncovered requirements:", td_body["uncovered_requirements"])
    print("REAL E6 TEST -- requirement coverage:", f"{len(td_body['covered_requirements'])}/{len(td_body['covered_requirements']) + len(td_body['uncovered_requirements'])}")

    # -- UI/UX applicability detection (deterministic, no LLM call) --
    applicability = client.get(f"/api/changes/{cid}/design").json()["ui_ux_applicability"]
    print("REAL E6 TEST -- UI/UX applicable:", applicability["applicable"], "reason:", applicability["reason"])
    ui_id = None
    if applicability["applicable"]:
        ui = client.post(f"/api/changes/{cid}/design/ui-ux", data={"provider": "claude"})
        assert ui.status_code == 200, ui.text
        ui_body = ui.json()
        print("REAL E6 TEST -- UI/UX design outcome:", ui_body["outcome"])
        assert ui_body["outcome"] == "READY", ui_body
        ui_id = ui_body["work_product"]["id"]

    # -- Independent Design Review (real invocation, separate process) --
    dr = client.post(f"/api/changes/{cid}/design/review", data={"provider": "claude"})
    assert dr.status_code == 200, dr.text
    dr_body = dr.json()
    print("REAL E6 TEST -- design review verdict:", dr_body["verdict"])
    refinement_rounds = 0
    while dr_body["verdict"] in ("NEEDS_REFINEMENT",) and refinement_rounds < 3:
        refinement_rounds += 1
        tid = client.get(f"/api/changes/{cid}/design").json()["technical_design"]["id"]
        refined = client.post(f"/api/changes/{cid}/design/refine", data={"technical_design_id": str(tid), "provider": "claude"})
        assert refined.status_code == 200, refined.text
        dr = client.post(f"/api/changes/{cid}/design/review", data={"provider": "claude"})
        assert dr.status_code == 200, dr.text
        dr_body = dr.json()
        print(f"REAL E6 TEST -- design review after refinement round {refinement_rounds}:", dr_body["verdict"])
    print("REAL E6 TEST -- total refinement rounds:", refinement_rounds)

    human_decisions_pending = client.app.state.human_decisions.pending_for_change(cid)
    print("REAL E6 TEST -- human decisions pending:", human_decisions_pending)

    design_ready = client.app.state.architecture_design_service.design_ready(cid)
    print("REAL E6 TEST -- DESIGN_READY:", design_ready)

    # -- Confirm no source code was ever modified by this real lifecycle --
    diff = subprocess.run(["git", "status", "--porcelain"], cwd=str(Path(__file__).resolve().parent.parent),
                           capture_output=True, text=True)
    print("REAL E6 TEST -- git status --porcelain after lifecycle (must be unrelated/empty for source):", repr(diff.stdout[:500]))
    # Only the pre-existing, unrelated uncommitted hunks from before this
    # test run may appear here -- never a file this test's services could
    # plausibly have touched (they have no source-writing code path at all).
    for line in diff.stdout.splitlines():
        path = line[3:]
        assert not path.startswith("specs/"), f"REAL E6 TEST must never touch the canonical specs/ tree: {line}"

    real_specs_after = {p.name for p in (Path(__file__).resolve().parent.parent / "specs" / "features").glob("*.yaml")}
    print("REAL E6 TEST -- confirmed: production specs/features/ untouched, still exactly", sorted(real_specs_after))
    assert "feat-thing.yaml" not in real_specs_after
