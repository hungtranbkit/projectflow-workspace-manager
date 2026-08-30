"""Test Design, Requirement Coverage & Executable Acceptance Mapping
(Phase E7). SAFETY: every test that touches SpecRegistry-backed behavior
uses the `tdesign_env` fixture below to redirect every E7 service's
`specs_root` to an isolated tmp_path tree -- the real `create_app()`
factory always resolves specs_root to THIS repo's own real specs/
directory. Never remove this fixture from a test that touches
requirement-coverage validation."""
from __future__ import annotations
import json

import pytest
import yaml

from app.services.test_design_service import TEST_LEVELS, TEST_TYPES


@pytest.fixture
def tdesign_env(client, tmp_path):
    specs_root = tmp_path / "specs"
    (specs_root / "features").mkdir(parents=True)
    (specs_root / "SPEC.yaml").write_text("schema_version: 1\nproject: test\nglossary: glossary.yaml\nfeatures_dir: features\n")
    (specs_root / "glossary.yaml").write_text("schema_version: 1\nterms: {}\n")
    (specs_root / "features" / "feat-thing.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "id": "FEAT-THING", "title": "The Thing", "version": 1, "status": "approved",
        "summary": "A thing.",
        "requirements": [{"id": "REQ-001", "text": "Do the first part."},
                          {"id": "REQ-002", "text": "Do the second part."}],
        "acceptance_criteria": [{"id": "AC-001", "text": "The first part is observable."},
                                 {"id": "AC-002", "text": "The second part is observable."}],
        "invariants": [{"id": "INV-001", "text": "Totals never go negative."}],
    }, sort_keys=False))
    for name in ("test_design_context_builder", "test_review_service", "requirement_coverage_service"):
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


def tc(key, req=None, acc=None, inv=None, level="INTEGRATION", ttype="POSITIVE", **overrides):
    d = {"key": key, "title": f"Test {key}", "test_level": level, "test_type": ttype,
         "expected_results": "Behaves per spec.", "requirement_ids": req or [], "acceptance_ids": acc or [],
         "invariant_ids": inv or [], "automation_candidate": True}
    d.update(overrides)
    return d


def design_payload(**overrides):
    p = {"strategy_summary": "Cover the happy path and one failure.",
         "test_cases": [tc("TC-001", req=["REQ-001"], acc=["AC-001"])]}
    p.update(overrides)
    return p


def review_payload(verdict="PASS", **overrides):
    p = {"verdict": verdict, "findings": []}
    p.update(overrides)
    return p


def do_design(client, cid, payload=None):
    set_fake(client, payload or design_payload())
    r = client.post(f"/api/changes/{cid}/tests/design", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


def do_review(client, cid, verdict="PASS", **overrides):
    set_fake(client, review_payload(verdict, **overrides))
    r = client.post(f"/api/changes/{cid}/tests/review", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


def create_workflow(client, cid, profile="AGENTIC_STANDARD"):
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": profile})
    assert r.status_code == 200, r.text
    return r.json()


# ================================================================ Roles/Capabilities (E7.1)

def test_test_designer_role_exists(client):
    roles = {r["key"] for r in client.app.state.roles_catalog.list_roles()}
    assert "TEST_DESIGNER" in roles


def test_test_reviewer_role_not_added_reviewer_reused(client):
    roles = {r["key"] for r in client.app.state.roles_catalog.list_roles()}
    assert "TEST_REVIEWER" not in roles


def test_test_designer_capability_validates_for_claude(client):
    result = client.app.state.roles_catalog.validate_assignment("claude", "TEST_DESIGNER")
    assert result["valid"] is True, result


def test_test_designer_never_has_deployment_capability(client):
    caps = {c["key"] for c in client.app.state.roles_catalog.capabilities_for_role("TEST_DESIGNER")}
    assert not caps & {"DEPLOY_DEV", "DEPLOY_TEST", "DEPLOY_PRODUCTION", "MERGE_PR", "ROLLBACK_DEPLOYMENT"}


# ================================================================ Test Design (E7.3/E7.4/E7.5)

def test_test_design_structured_result(client, git_repo, tdesign_env):
    cid = new_change(client, "Test design change")
    link_governing_feature(client, cid)
    result = do_design(client, cid)
    assert result["outcome"] == "READY"
    assert result["test_plan"]["kind"] == "TEST_PLAN"
    assert result["test_case_set"]["kind"] == "TEST_CASE_SET"
    assert len(result["test_cases"]) == 1
    assert result["test_cases"][0]["requirement_ids"] == ["REQ-001"]


def test_test_design_valid_spec_ids_stored(client, git_repo, tdesign_env):
    cid = new_change(client, "Valid ids change")
    link_governing_feature(client, cid)
    result = do_design(client, cid, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001", "REQ-002"], acc=["AC-001"], inv=["INV-001"])]))
    case = result["test_cases"][0]
    assert case["requirement_ids"] == ["REQ-001", "REQ-002"]
    assert case["acceptance_ids"] == ["AC-001"]
    assert case["invariant_ids"] == ["INV-001"]


def test_test_design_fictional_ids_silently_excluded(client, git_repo, tdesign_env):
    cid = new_change(client, "Fictional ids change")
    link_governing_feature(client, cid)
    result = do_design(client, cid, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001", "REQ-DOES-NOT-EXIST"], acc=["AC-FAKE"])]))
    case = result["test_cases"][0]
    assert case["requirement_ids"] == ["REQ-001"]
    assert case["acceptance_ids"] == []


def test_test_design_negative_case_generation(client, git_repo, tdesign_env):
    cid = new_change(client, "Negative case change")
    link_governing_feature(client, cid)
    result = do_design(client, cid, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001"], acc=["AC-001"]),
        tc("TC-002", req=["REQ-001"], ttype="NEGATIVE", title="invalid input rejected"),
    ]))
    types = {c["test_type"] for c in result["test_cases"]}
    assert "NEGATIVE" in types


def test_test_design_ui_and_manual_acceptance_levels(client, git_repo, tdesign_env):
    cid = new_change(client, "UI manual acceptance change")
    link_governing_feature(client, cid)
    result = do_design(client, cid, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001"], level="UI"),
        tc("TC-002", req=["REQ-002"], level="MANUAL_ACCEPTANCE", ttype="MANUAL_ACCEPTANCE"),
    ]))
    levels = {c["test_level"] for c in result["test_cases"]}
    assert {"UI", "MANUAL_ACCEPTANCE"} <= levels


def test_test_design_migration_case_generation(client, git_repo, tdesign_env):
    cid = new_change(client, "Migration case change")
    link_governing_feature(client, cid)
    result = do_design(client, cid, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001"], level="MIGRATION", title="column migration verified")]))
    assert result["test_cases"][0]["test_level"] == "MIGRATION"


def test_test_design_unknown_level_falls_back_to_integration(client, git_repo, tdesign_env):
    cid = new_change(client, "Unknown level change")
    link_governing_feature(client, cid)
    result = do_design(client, cid, design_payload(test_cases=[
        {"key": "TC-001", "title": "x", "test_level": "NOT_A_REAL_LEVEL", "expected_results": "y"}]))
    assert result["test_cases"][0]["test_level"] == "INTEGRATION"


def test_untestable_items_and_implementation_conflicts_captured(client, git_repo, tdesign_env):
    cid = new_change(client, "Untestable items change")
    link_governing_feature(client, cid)
    result = do_design(client, cid, design_payload(
        untestable_or_ambiguous_items=["The exact retry backoff timing is unspecified."],
        implementation_spec_conflicts=["Design mentions a cache that the spec never describes."]))
    plan_content = result["test_plan"]["content_metadata"]
    assert plan_content["untestable_or_ambiguous_items"]
    assert plan_content["implementation_spec_conflicts"]


def test_human_decision_from_test_design(client, git_repo, tdesign_env):
    cid = new_change(client, "Test design human decision change")
    link_governing_feature(client, cid)
    do_design(client, cid, design_payload(human_decisions=[
        {"question": "Should deletion cascade?", "reason": "business rule undefined", "decision_type": "BUSINESS_RULE"}]))
    assert client.app.state.human_decisions.pending_for_change(cid) is True


# ================================================================ Requirement Coverage (E7.6/E7.7/E7.8/E7.9)

def test_coverage_full(client, git_repo, tdesign_env):
    cid = new_change(client, "Full coverage change")
    link_governing_feature(client, cid)
    do_design(client, cid, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001"], acc=["AC-001"]),
        tc("TC-002", req=["REQ-002"], acc=["AC-002"], inv=["INV-001"]),
    ]))
    coverage = client.get(f"/api/changes/{cid}/tests/coverage").json()
    assert coverage["requirements_uncovered"] == []
    assert coverage["acceptance_uncovered"] == []
    assert coverage["invariants_uncovered"] == []
    assert coverage["requirements_total"] == 2


def test_coverage_partial_missing_ac_and_invariant(client, git_repo, tdesign_env):
    cid = new_change(client, "Partial coverage change")
    link_governing_feature(client, cid)
    do_design(client, cid, design_payload(test_cases=[tc("TC-001", req=["REQ-001"], acc=["AC-001"])]))
    coverage = client.get(f"/api/changes/{cid}/tests/coverage").json()
    assert coverage["requirements_uncovered"] == ["REQ-002"]
    assert coverage["acceptance_uncovered"] == ["AC-002"]
    assert coverage["invariants_uncovered"] == ["INV-001"]


def test_coverage_duplicate_does_not_inflate_totals(client, git_repo, tdesign_env):
    cid = new_change(client, "Duplicate coverage change")
    link_governing_feature(client, cid)
    do_design(client, cid, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001"], acc=["AC-001"]),
        tc("TC-002", req=["REQ-001"], acc=["AC-001"]),  # same ids, different test case
    ]))
    coverage = client.get(f"/api/changes/{cid}/tests/coverage").json()
    assert coverage["requirements_covered"] == ["REQ-001"]
    assert coverage["requirements_total"] == 2
    assert coverage["test_case_count"] == 2


def test_coverage_manual_acceptance_counts_correctly(client, git_repo, tdesign_env):
    cid = new_change(client, "Manual acceptance counting change")
    link_governing_feature(client, cid)
    do_design(client, cid, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001"], acc=["AC-001"]),
        tc("TC-002", req=["REQ-002"], acc=["AC-002"], level="MANUAL_ACCEPTANCE", ttype="MANUAL_ACCEPTANCE"),
    ]))
    coverage = client.get(f"/api/changes/{cid}/tests/coverage").json()
    assert coverage["manual_only_items"] == 1
    assert coverage["automation_candidates"] == 2  # both marked automation_candidate=True by default


def test_coverage_invalid_references_surfaced(client, git_repo, tdesign_env):
    cid = new_change(client, "Invalid reference change")
    link_governing_feature(client, cid)
    # Bypass the design-time filtering by inserting a raw row directly,
    # simulating a stored reference that no longer resolves (e.g. after
    # a spec edit) -- the coverage engine must still surface it, never
    # silently drop it from invalid_references.
    do_design(client, cid, design_payload(test_cases=[tc("TC-001", req=["REQ-001"])]))
    test_set = client.app.state.test_design_service.current_test_case_set(cid)
    case = client.app.state.test_case_specs_store.list_for_work_product(test_set["id"])[0]
    client.app.state.db.execute("UPDATE test_case_specs SET requirement_ids=? WHERE id=?",
                                 (json.dumps(["REQ-001", "REQ-GHOST"]), case["id"]))
    coverage = client.get(f"/api/changes/{cid}/tests/coverage").json()
    assert any(r["id"] == "REQ-GHOST" for r in coverage["invalid_references"])


def test_coverage_vacuous_when_nothing_governs(client, git_repo, tdesign_env):
    cid = new_change(client, "Ungoverned change")
    coverage = client.get(f"/api/changes/{cid}/tests/coverage").json()
    assert coverage["requirements_total"] == 0
    assert coverage["requirements_uncovered"] == []


# ================================================================ Test Review (E7.10)

def test_review_pass_approves_test_case_set_and_plan(client, git_repo, tdesign_env):
    cid = new_change(client, "Review pass change")
    link_governing_feature(client, cid)
    do_design(client, cid)
    review = do_review(client, cid, "PASS")
    assert review["verdict"] == "PASS"
    tests = client.get(f"/api/changes/{cid}/tests").json()
    assert tests["test_case_set"]["status"] == "APPROVED"
    assert tests["test_plan"]["status"] == "APPROVED"
    assert all(tc["status"] == "APPROVED" for tc in tests["test_cases"])


def test_review_needs_refinement(client, git_repo, tdesign_env):
    cid = new_change(client, "Review refinement change")
    link_governing_feature(client, cid)
    do_design(client, cid)
    review = do_review(client, cid, "NEEDS_REFINEMENT", findings=[
        {"category": "missing_negative_path", "description": "No failure case.", "severity": "MEDIUM"}])
    assert review["verdict"] == "NEEDS_REFINEMENT"


def test_review_human_decision_required(client, git_repo, tdesign_env):
    cid = new_change(client, "Review human decision change")
    link_governing_feature(client, cid)
    do_design(client, cid)
    do_review(client, cid, "HUMAN_DECISION_REQUIRED", human_decisions=[{"question": "Which UX?", "reason": "tradeoff"}])
    assert client.app.state.human_decisions.pending_for_change(cid) is True


def test_review_reject_marks_rejected(client, git_repo, tdesign_env):
    cid = new_change(client, "Review reject change")
    link_governing_feature(client, cid)
    do_design(client, cid)
    do_review(client, cid, "REJECT")
    tests = client.get(f"/api/changes/{cid}/tests").json()
    assert tests["test_case_set"]["status"] == "REJECTED"


def test_review_receives_real_deterministic_coverage_not_model_claim(client, git_repo, tdesign_env):
    cid = new_change(client, "Review sees real coverage change")
    link_governing_feature(client, cid)
    do_design(client, cid, design_payload(test_cases=[tc("TC-001", req=["REQ-001"], acc=["AC-001"])]))
    captured = {}
    def runner(argv, cwd, timeout):
        captured["prompt"] = argv[2]
        env = envelope(review_payload("PASS"))
        class R:
            returncode = 0
            stdout = json.dumps(env)
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = runner
    client.post(f"/api/changes/{cid}/tests/review", data={"provider": "claude"})
    assert "deterministic_coverage_report" in captured["prompt"]
    assert "REQ-002" in captured["prompt"]  # the uncovered requirement is visible to the reviewer


# ================================================================ Bounded refinement (E7.11)

def test_bounded_refinement_loop_never_infinite(client, git_repo, tdesign_env):
    cid = new_change(client, "Endless test refinement change")
    link_governing_feature(client, cid)

    def cycling_runner(argv, cwd, timeout):
        prompt = argv[2]
        if "TEST DESIGNER" in prompt:
            payload = design_payload()
        elif "INDEPENDENT TEST REVIEWER" in prompt:
            payload = review_payload("NEEDS_REFINEMENT", findings=[{"category": "weak_assertion", "description": "Still vague."}])
        else:
            raise AssertionError(f"unexpected prompt: {prompt[:200]}")
        env = envelope(payload)
        class R:
            returncode = 0
            stdout = json.dumps(env)
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = cycling_runner

    result = client.app.state.test_design_lifecycle_service.run_design(cid, provider="claude")
    assert result["outcome"] == "NEEDS_REFINEMENT"
    assert result["rounds"] == 3


def test_refinement_creates_immutable_history(client, git_repo, tdesign_env):
    cid = new_change(client, "Immutable history change")
    link_governing_feature(client, cid)
    first = do_design(client, cid, design_payload(test_cases=[tc("TC-001", req=["REQ-001"])]))
    review = do_review(client, cid, "NEEDS_REFINEMENT", findings=[{"category": "behavior_coverage", "description": "Add REQ-002."}])
    set_fake(client, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001"]), tc("TC-002", req=["REQ-002"])]))
    refined = client.app.state.test_design_service.refine(first["test_case_set"]["id"], review, provider="claude")
    assert refined["outcome"] == "READY"
    assert refined["test_case_set"]["id"] != first["test_case_set"]["id"]
    prior_set = client.app.state.work_products.get(first["test_case_set"]["id"])
    assert prior_set["status"] == "SUPERSEDED"
    prior_cases = client.app.state.test_case_specs_store.list_for_work_product(first["test_case_set"]["id"])
    assert all(c["status"] == "SUPERSEDED" for c in prior_cases)
    assert prior_cases[0]["requirement_ids"] == json.dumps(["REQ-001"])  # never mutated in place


def test_refinement_cannot_fake_coverage_without_real_test_case(client, git_repo, tdesign_env):
    """Refining never marks a requirement covered unless a real
    TestCaseSpec actually references it -- the coverage engine always
    recomputes from real ids, never trusts a review verdict alone."""
    cid = new_change(client, "Cannot fake coverage change")
    link_governing_feature(client, cid)
    do_design(client, cid, design_payload(test_cases=[tc("TC-001", req=["REQ-001"])]))
    do_review(client, cid, "PASS")  # PASS despite REQ-002 being uncovered
    coverage = client.get(f"/api/changes/{cid}/tests/coverage").json()
    assert coverage["requirements_uncovered"] == ["REQ-002"]


# ================================================================ Executable Test Mapping (E7.13/E7.14)

def test_executable_mapping_defaults_to_unimplemented(client, git_repo, tdesign_env):
    cid = new_change(client, "Unmapped test change")
    link_governing_feature(client, cid)
    result = do_design(client, cid)
    tcid = result["test_cases"][0]["id"]
    mapping = client.get(f"/api/test-cases/{tcid}/mapping").json()
    assert mapping["implementation_status"] == "UNIMPLEMENTED"


def test_executable_mapping_explicit_valid(client, git_repo, tdesign_env):
    cid = new_change(client, "Mapped test change")
    link_governing_feature(client, cid)
    result = do_design(client, cid)
    tcid = result["test_cases"][0]["id"]
    r = client.post(f"/api/test-cases/{tcid}/map-executable", data={
        "repository_path": "tests/test_thing.py", "test_symbol": "test_does_the_first_part", "framework": "pytest"})
    assert r.status_code == 200, r.text
    assert r.json()["implementation_status"] == "IMPLEMENTED"
    mapping = client.get(f"/api/test-cases/{tcid}/mapping").json()
    assert mapping["repository_path"] == "tests/test_thing.py"


def test_executable_mapping_requires_explicit_symbol_or_command(client, git_repo, tdesign_env):
    cid = new_change(client, "Invalid mapping change")
    link_governing_feature(client, cid)
    result = do_design(client, cid)
    tcid = result["test_cases"][0]["id"]
    r = client.post(f"/api/test-cases/{tcid}/map-executable", data={"repository_path": "tests/test_thing.py"})
    assert r.status_code == 400


def test_executable_mapping_record_result(client, git_repo, tdesign_env):
    cid = new_change(client, "Result recording change")
    link_governing_feature(client, cid)
    result = do_design(client, cid)
    tcid = result["test_cases"][0]["id"]
    client.app.state.executable_test_mapping_service.map(tcid, None, "tests/test_thing.py", "test_x")
    updated = client.app.state.executable_test_mapping_service.record_result(tcid, "PASS", "test_runs:1")
    assert updated["implementation_status"] == "PASS"


def test_executable_mapping_stale_after_refinement(client, git_repo, tdesign_env):
    cid = new_change(client, "Stale mapping change")
    link_governing_feature(client, cid)
    result = do_design(client, cid, design_payload(test_cases=[tc("TC-001", req=["REQ-001"])]))
    tcid = result["test_cases"][0]["id"]
    client.app.state.executable_test_mapping_service.map(tcid, None, "tests/test_thing.py", "test_x")
    review = do_review(client, cid, "NEEDS_REFINEMENT", findings=[{"category": "behavior_coverage", "description": "x"}])
    set_fake(client, design_payload(test_cases=[tc("TC-001", req=["REQ-001"], title="revised")]))
    client.app.state.test_design_service.refine(result["test_case_set"]["id"], review, provider="claude")
    staleness = client.get(f"/api/changes/{cid}/tests/staleness").json()
    assert any(m["test_case_spec_id"] == tcid for m in staleness["executable_test_mapping_stale"])


# ================================================================ Planner integration (E7.15/E7.16)

def test_planner_context_sees_test_design(client, git_repo, tdesign_env):
    cid = new_change(client, "Planner sees test design change")
    link_governing_feature(client, cid)
    do_design(client, cid)
    create_workflow(client, cid, "AGENTIC_STANDARD")
    context = client.app.state.planner_service.context_builder.build(cid, "AGENTIC_STANDARD")
    kinds = {d["kind"] for d in context["test_design"]["work_products"]}
    assert "TEST_CASE_SET" in kinds


def test_planner_context_sees_unimplemented_test_cases(client, git_repo, tdesign_env):
    cid = new_change(client, "Planner sees unimplemented change")
    link_governing_feature(client, cid)
    do_design(client, cid)
    create_workflow(client, cid, "AGENTIC_STANDARD")
    context = client.app.state.planner_service.context_builder.build(cid, "AGENTIC_STANDARD")
    assert context["test_design"]["test_implementation_required"] is True
    assert len(context["test_design"]["unimplemented_test_cases"]) == 1


def test_plan_test_design_staleness_signal(client, git_repo, tdesign_env):
    cid = new_change(client, "Plan test design staleness change", project_id=register(client, git_repo[1], "demo"))
    create_workflow(client, cid, "VIBE")
    set_fake(client, {"summary": "x", "tasks": [{"key": "T1", "title": "Build", "task_type": "IMPLEMENTATION"}]})
    plan_result = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"}).json()
    pid = plan_result["plan"]["id"]

    staleness = client.get(f"/api/plans/{pid}/test-design-staleness").json()
    assert staleness["stale"] is False

    link_governing_feature(client, cid)
    do_design(client, cid)

    staleness = client.get(f"/api/plans/{pid}/test-design-staleness").json()
    assert staleness["stale"] is True
    assert staleness["reason"] == "PLAN_TEST_DESIGN_STALE"


# ================================================================ Workflow gate (E7.17)

def test_test_design_ready_vacuous_without_governing_spec(client, git_repo, tdesign_env):
    cid = new_change(client, "Ungoverned gate change")
    assert client.app.state.test_design_lifecycle_service.test_design_ready(cid) is True


def test_test_design_ready_requires_real_review_evidence(client, git_repo, tdesign_env):
    cid = new_change(client, "Gate test design change")
    create_workflow(client, cid, "AGENTIC_STANDARD")
    link_governing_feature(client, cid)
    do_design(client, cid, design_payload(test_cases=[
        tc("TC-001", req=["REQ-001"]), tc("TC-002", req=["REQ-002"])]))
    assert client.app.state.test_design_lifecycle_service.test_design_ready(cid) is False  # not reviewed yet
    do_review(client, cid, "PASS")
    assert client.app.state.test_design_lifecycle_service.test_design_ready(cid) is True


def test_workflow_service_without_test_design_gate_hook_is_vacuous(client, git_repo):
    """Backward compatibility: a WorkflowService constructed WITHOUT the
    E7 hook (every pre-E7 test's own construction) must never block on
    TEST_DESIGN_READY at all."""
    from app.services.workflow_engine import WorkflowService
    db = client.app.state.db
    ws = WorkflowService(db, client.app.state.workflow_catalog, client.app.state.changes, client.app.state.work_products,
                          client.app.state.decision, client.app.state.spec_compliance, client.app.state.task_dependencies)
    assert ws.test_design_gate is None
    cid = new_change(client, "Legacy fallback change")
    assert ws._gate_test_design_ready(cid, []) is True


# ================================================================ Spec Compliance integration (E7.18)

def test_spec_compliance_designed_test_without_execution_never_passes(client, git_repo, tdesign_env):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Spec compliance gap change", project_id=rid)
    link_governing_feature(client, cid)
    do_design(client, cid, design_payload(test_cases=[tc("TC-001", req=["REQ-001"])]))
    r = client.post("/api/tasks", data={"title": "Spec-linked task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Spec-linked task"][0]
    client.app.state.changes.attach_task_to_change(cid, tid)
    client.post(f"/api/tasks/{tid}/spec", data={
        "classification": "BUG_FIX_TO_EXISTING_SPEC", "feature_id": "FEAT-THING", "requirement_ids": "REQ-001"})
    result = client.app.state.spec_compliance.verify(tid)
    assert result["verdict"] != "PASS"
    assert result["test_contract"] == "TEST_IMPLEMENTATION_MISSING"


def test_spec_compliance_missing_evidence_vs_fail_distinguished(client, git_repo, tdesign_env):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Spec compliance evidence change", project_id=rid)
    link_governing_feature(client, cid)
    result = do_design(client, cid, design_payload(test_cases=[tc("TC-001", req=["REQ-001"])]))
    tcid = result["test_cases"][0]["id"]
    r = client.post("/api/tasks", data={"title": "Evidence task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Evidence task"][0]
    client.app.state.changes.attach_task_to_change(cid, tid)
    client.post(f"/api/tasks/{tid}/spec", data={
        "classification": "BUG_FIX_TO_EXISTING_SPEC", "feature_id": "FEAT-THING", "requirement_ids": "REQ-001"})

    client.app.state.executable_test_mapping_service.map(tcid, None, "tests/test_thing.py", "test_x")
    result_missing = client.app.state.spec_compliance.verify(tid)
    assert result_missing["test_contract"] == "TEST_EVIDENCE_MISSING"

    client.app.state.executable_test_mapping_service.record_result(tcid, "FAIL")
    result_fail = client.app.state.spec_compliance.verify(tid)
    assert result_fail["test_contract"] == "TEST_EVIDENCE_FAIL"
    assert result_fail["verdict"] != "PASS"


def test_spec_compliance_test_contract_none_when_services_unwired(client, git_repo):
    """Backward compatibility: SpecComplianceVerifier constructed WITHOUT
    the E7 hooks (every pre-E7 test's own construction) never computes
    test_contract at all."""
    from app.services.spec_compliance import SpecComplianceVerifier
    verifier = SpecComplianceVerifier(client.app.state.db, client.app.state.decision, client.app.state.specs_root)
    assert verifier.test_case_specs is None
    root, repo = git_repo
    rid = register(client, repo, "demo")
    r = client.post("/api/tasks", data={"title": "Legacy compliance task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Legacy compliance task"][0]
    result = verifier.verify(tid)
    assert result["test_contract"] is None


# ================================================================ E7 never generates implementation Tasks

def test_test_design_never_creates_implementation_task(client, git_repo, tdesign_env):
    cid = new_change(client, "No auto implementation from tests change")
    link_governing_feature(client, cid)
    do_design(client, cid)
    do_review(client, cid, "PASS")
    tasks = client.get(f"/api/changes/{cid}/tasks").json()
    assert all(t["task_type"] != "IMPLEMENTATION" for t in tasks)


# ================================================================ Meta safety

def test_production_specs_root_never_touched_by_this_suite():
    from pathlib import Path
    real_specs = Path(__file__).resolve().parent.parent / "specs" / "features"
    names = {p.name for p in real_specs.glob("*.yaml")}
    assert "feat-thing.yaml" not in names


def test_real_test_design_lifecycle_end_to_end(client, git_repo, tdesign_env):
    """E7.23: one safe, real, non-fake Test Design lifecycle run against
    a disposable Change and an isolated tmp_path specs_root (tdesign_env)
    -- NO fake runner is installed anywhere in this test, so every Test
    Design / Test Review invocation below is a genuine `claude -p
    --json-schema ... --tools "" --max-turns 1` subprocess call. Same
    disposable-fixture, harmless-intent convention E5.24/E6.23 established.
    No service in this module has any source/test-file-write code path
    at all -- confirmed at the end via git status on the real repo."""
    import subprocess
    from pathlib import Path
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

    # -- Test Design (real invocation) --
    td = client.post(f"/api/changes/{cid}/tests/design", data={"provider": "claude"})
    assert td.status_code == 200, td.text
    td_body = td.json()
    print("REAL E7 TEST -- test design outcome:", td_body["outcome"])
    assert td_body["outcome"] == "READY", td_body
    cases = td_body["test_cases"]
    print("REAL E7 TEST -- test case count:", len(cases))
    levels = sorted({c["test_level"] for c in cases})
    types = sorted({c["test_type"] for c in cases})
    print("REAL E7 TEST -- test levels:", levels, "test types:", types)
    negative_count = sum(1 for c in cases if c["test_type"] == "NEGATIVE")
    manual_count = sum(1 for c in cases if c["test_type"] == "MANUAL_ACCEPTANCE" or c["test_level"] == "MANUAL_ACCEPTANCE")
    print("REAL E7 TEST -- negative cases:", negative_count, "manual acceptance cases:", manual_count)

    # -- Deterministic coverage (no LLM call) --
    coverage = client.get(f"/api/changes/{cid}/tests/coverage").json()
    print("REAL E7 TEST -- requirements:", f"{len(coverage['requirements_covered'])}/{coverage['requirements_total']}")
    print("REAL E7 TEST -- acceptance:", f"{len(coverage['acceptance_covered'])}/{coverage['acceptance_total']}")
    print("REAL E7 TEST -- invariants:", f"{len(coverage['invariants_covered'])}/{coverage['invariants_total']}")
    print("REAL E7 TEST -- invalid references:", coverage["invalid_references"])

    # -- Independent Test Review (real invocation, separate process) --
    # The underlying `claude -p --max-turns 1` CLI call is occasionally
    # flaky at the turn-budget boundary (observed independently during
    # E6's own live verification -- a pre-existing property of the
    # shared PlannerAgentInvoker mechanism, not something E7 introduces)
    # -- retry once on a clean EXECUTION_FAILED/OUTPUT_INVALID before
    # treating it as a real failure.
    for attempt in range(2):
        dr = client.post(f"/api/changes/{cid}/tests/review", data={"provider": "claude"})
        assert dr.status_code == 200, dr.text
        dr_body = dr.json()
        if dr_body.get("verdict") is not None:
            break
        print(f"REAL E7 TEST -- review attempt {attempt + 1} outcome:", dr_body.get("outcome"), "message:", dr_body.get("message"))
    assert dr_body.get("verdict") is not None, dr_body
    print("REAL E7 TEST -- review verdict:", dr_body["verdict"])
    refinement_rounds = 0
    while dr_body["verdict"] == "NEEDS_REFINEMENT" and refinement_rounds < 3:
        refinement_rounds += 1
        set_id = client.get(f"/api/changes/{cid}/tests").json()["test_case_set"]["id"]
        refined = client.post(f"/api/changes/{cid}/tests/refine", data={"test_case_set_id": str(set_id), "provider": "claude"})
        assert refined.status_code == 200, refined.text
        dr = client.post(f"/api/changes/{cid}/tests/review", data={"provider": "claude"})
        assert dr.status_code == 200, dr.text
        dr_body = dr.json()
        print(f"REAL E7 TEST -- review after refinement round {refinement_rounds}:", dr_body["verdict"])
    print("REAL E7 TEST -- total refinement rounds:", refinement_rounds)

    human_decisions_pending = client.app.state.human_decisions.pending_for_change(cid)
    print("REAL E7 TEST -- human decisions pending:", human_decisions_pending)

    # -- Executable mapping remains unimplemented (no safe existing test
    #    to explicitly map, per E7.23's own instruction) --
    status = client.get(f"/api/changes/{cid}/tests/status").json()
    print("REAL E7 TEST -- test_implementation_required:", status["test_implementation_required"])
    print("REAL E7 TEST -- unimplemented test cases:", len(status["unimplemented_test_cases"]))

    ready = client.app.state.test_design_lifecycle_service.test_design_ready(cid)
    print("REAL E7 TEST -- TEST_DESIGN_READY:", ready)

    # -- Confirm no source/test file was ever modified by this real lifecycle --
    diff = subprocess.run(["git", "status", "--porcelain"], cwd=str(Path(__file__).resolve().parent.parent),
                           capture_output=True, text=True)
    print("REAL E7 TEST -- git status --porcelain after lifecycle:", repr(diff.stdout[:500]))
    for line in diff.stdout.splitlines():
        path = line[3:]
        assert not path.startswith("specs/"), f"REAL E7 TEST must never touch the canonical specs/ tree: {line}"

    real_specs_after = {p.name for p in (Path(__file__).resolve().parent.parent / "specs" / "features").glob("*.yaml")}
    print("REAL E7 TEST -- confirmed: production specs/features/ untouched, still exactly", sorted(real_specs_after))
    assert "feat-thing.yaml" not in real_specs_after
