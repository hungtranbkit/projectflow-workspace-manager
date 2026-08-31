"""Workflow / Process Engine (Phase E3). Covers the three deliberately
separate layers -- WORKFLOW DEFINITION (catalog), WORKFLOW INSTANCE
(WorkflowRun, durable but only ever storing identity), and TASK
EXECUTION (untouched: TaskDecisionService/_start_builder_session) --
plus the Task dependency graph and backward compatibility."""
from __future__ import annotations
import json

import pytest

from app.launchers import AgentLauncher
from app.services.workflow_engine import (
    GATES, PROFILES, PROFILE_STAGES, STAGE_ORDER, TASK_TYPES, WorkflowError,
)


def register(client, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    return client.get("/api/repositories").json()[0]["id"]


def new_task(client, title, risk="LOW"):
    r = client.post("/api/tasks", data={"title": title, "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303
    return [t["id"] for t in client.get("/api/tasks").json() if t["title"] == title][0]


def add_workspace(client, tid, rid, agent="codex", role="Backend"):
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": agent, "role": role, "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return [w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["agent"] == agent and w["role"] == role][-1]


def setup_fast_ready_launcher(client, agent="codex"):
    client.app.state.agent_sessions.launchers = {agent: AgentLauncher(agent.capitalize(), "bash", ("-c", "echo READY; cat"))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1


def new_change(client, title, project_id=None):
    data = {"title": title}
    if project_id is not None:
        data["project_id"] = str(project_id)
    r = client.post("/api/changes", data=data)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def create_workflow(client, cid, profile_key):
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": profile_key})
    assert r.status_code == 200, r.text
    return r.json()


def full_review_pass(client, tid, rid, agent="codex"):
    """A Task carried through Select -> Start -> Submit -> Review PASS
    -- real evidence, not fixture-injected."""
    setup_fast_ready_launcher(client, agent)
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent=agent)
    client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": agent})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    return w


# ============================================================== TaskType

def test_task_type_catalog_loads_with_stable_keys(client):
    types = {t["key"]: t for t in client.get("/api/engineering/task-types").json()}
    for key in TASK_TYPES:
        assert key in types, f"missing seeded task type {key}"
    assert len(types) == len(TASK_TYPES)


def test_task_type_role_mapping_matches_worked_examples(client):
    def preferred(key):
        return client.get(f"/api/engineering/task-types/{key}").json()["preferred_role_key"]
    assert preferred("IMPLEMENTATION") == "BUILDER"
    assert preferred("CODE_REVIEW") == "REVIEWER"
    assert preferred("SECURITY_REVIEW") == "SECURITY_REVIEWER"
    assert preferred("RELEASE") == "RELEASE_MANAGER"
    assert preferred("SPEC_AUTHORING") == "SPEC_ANALYST"
    # UI_UX_DESIGN: E3 seeded this with no matching role ("never forced
    # onto an unrelated one"); Phase E6 added the real UI_UX_DESIGNER
    # role, completing this placeholder rather than redefining it.
    assert preferred("UI_UX_DESIGN") == "UI_UX_DESIGNER"


def test_task_type_get_404_for_unknown_key(client):
    assert client.get("/api/engineering/task-types/NOT_A_TYPE").status_code == 404


def test_legacy_task_has_no_task_type_by_default(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Untyped legacy task")
    t = client.get(f"/api/tasks/{tid}").json()
    assert t["task_type"] is None


def test_set_and_clear_task_type(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Typed task")
    r = client.post(f"/api/tasks/{tid}/task-type", data={"task_type": "implementation"})
    assert r.status_code == 200
    assert r.json()["task_type"] == "IMPLEMENTATION"
    r2 = client.post(f"/api/tasks/{tid}/task-type", data={"task_type": ""})
    assert r2.json()["task_type"] is None


def test_set_unknown_task_type_rejected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Bad type task")
    r = client.post(f"/api/tasks/{tid}/task-type", data={"task_type": "NOT_A_TYPE"})
    assert r.status_code == 400


# ========================================================= Workflow profiles

def test_all_three_profiles_seeded(client):
    profiles = {p["key"] for p in client.get("/api/engineering/workflow-profiles").json()}
    assert profiles == {"VIBE", "AGENTIC_STANDARD", "CONTROLLED"}


def test_vibe_minimum_path_is_build_verify_only(client):
    stages = {s["stage_key"]: s["requirement"] for s in
               next(p for p in client.get("/api/engineering/workflow-profiles").json() if p["key"] == "VIBE")["stages"]}
    assert stages["BUILD"] == "REQUIRED"
    assert stages["VERIFY"] == "REQUIRED"
    assert stages.get("SPEC") is None  # not part of VIBE at all
    assert stages.get("ARCHITECTURE") is None
    assert stages["REVIEW"] == "OPTIONAL"
    assert stages["DEPLOY"] == "REQUIRED_IF"


def test_agentic_standard_requires_spec_build_review_verify(client):
    stages = {s["stage_key"]: s["requirement"] for s in
               next(p for p in client.get("/api/engineering/workflow-profiles").json() if p["key"] == "AGENTIC_STANDARD")["stages"]}
    for required in ("SPEC", "BUILD", "REVIEW", "VERIFY"):
        assert stages[required] == "REQUIRED"
    assert stages["ARCHITECTURE"] == "OPTIONAL"
    assert stages["DESIGN"] == "OPTIONAL"


def test_controlled_requires_stronger_gates(client):
    stages = {s["stage_key"]: s["requirement"] for s in
               next(p for p in client.get("/api/engineering/workflow-profiles").json() if p["key"] == "CONTROLLED")["stages"]}
    for required in ("SPEC", "DESIGN", "BUILD", "REVIEW", "VERIFY", "RELEASE", "HUMAN_ACCEPTANCE"):
        assert stages[required] == "REQUIRED"


def test_default_profile_is_agentic_standard_when_unspecified(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    cid = new_change(client, "Default profile change")
    run = create_workflow(client, cid, "")
    assert run["profile_key"] == "AGENTIC_STANDARD"


def test_gate_requirements_never_require_merge_pr_or_deploy_production():
    """E2's own rule extends to the workflow layer: no GateRequirement
    silently demands a sensitive, human-gated capability."""
    for key in GATES:
        assert key not in ("MERGE_PR", "DEPLOY_PRODUCTION")


# ========================================================= Workflow creation

def test_create_workflow_for_change(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    cid = new_change(client, "Real workflow change")
    run = create_workflow(client, cid, "CONTROLLED")
    assert run["change_id"] == cid
    assert run["profile_key"] == "CONTROLLED"
    fetched = client.get(f"/api/changes/{cid}/workflow").json()
    assert fetched["id"] == run["id"]


def test_cannot_create_a_second_workflow_for_the_same_change(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    cid = new_change(client, "Once only change")
    create_workflow(client, cid, "VIBE")
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "CONTROLLED"})
    assert r.status_code == 400


def test_unknown_profile_rejected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    cid = new_change(client, "Bad profile change")
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "NOT_A_REAL_PROFILE"})
    assert r.status_code == 400


def test_get_workflow_404_before_creation(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    cid = new_change(client, "No workflow yet")
    assert client.get(f"/api/changes/{cid}/workflow").status_code == 404


def test_workflow_creation_for_unknown_change_404(client):
    assert client.post("/api/changes/999999/workflow", data={}).status_code == 404


def test_project_policy_restricts_allowed_profiles(client, git_repo):
    root, repo = git_repo
    (repo / "PROJECT.yaml").write_text(
        (repo / "PROJECT.yaml").read_text()
        + "\nengineering:\n  workflow:\n    default_profile: VIBE\n    allowed_profiles: [VIBE]\n"
    )
    import subprocess
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "workflow policy"], cwd=repo, check=True)
    rid = register(client, repo, "demo")

    cid = new_change(client, "Policy-restricted change", project_id=rid)
    # explicit profile blank -> policy default (VIBE) is used
    run = create_workflow(client, cid, "")
    assert run["profile_key"] == "VIBE"

    cid2 = new_change(client, "Policy-blocked change", project_id=rid)
    r = client.post(f"/api/changes/{cid2}/workflow", data={"profile_key": "CONTROLLED"})
    assert r.status_code == 400
    assert "restricts workflows" in r.json()["message"]


def test_no_project_policy_uses_safe_global_default(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "No policy change", project_id=rid)
    run = create_workflow(client, cid, "")
    assert run["profile_key"] == "AGENTIC_STANDARD"


# ============================================================ Dependencies

def test_simple_dependency_and_readiness(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    a = new_task(client, "Dep A")
    b = new_task(client, "Dep B")
    r = client.post(f"/api/tasks/{b}/dependencies", data={"depends_on_task_id": a})
    assert r.status_code == 200
    readiness = client.get(f"/api/tasks/{b}/dependencies").json()["readiness"]
    assert readiness["readiness"] == "WAITING_DEPENDENCY"
    assert readiness["unmet_dependencies"] == [a]

    a_readiness = client.get(f"/api/tasks/{a}/dependencies").json()["readiness"]
    assert a_readiness["readiness"] == "READY"


def test_multiple_prerequisites_all_must_complete(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    a = new_task(client, "Multi A")
    b = new_task(client, "Multi B")
    c = new_task(client, "Multi C")
    client.post(f"/api/tasks/{c}/dependencies", data={"depends_on_task_id": a})
    client.post(f"/api/tasks/{c}/dependencies", data={"depends_on_task_id": b})
    readiness = client.get(f"/api/tasks/{c}/dependencies").json()["readiness"]
    assert readiness["readiness"] == "WAITING_DEPENDENCY"
    assert set(readiness["unmet_dependencies"]) == {a, b}


def test_duplicate_dependency_prevented(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    a = new_task(client, "Dup A")
    b = new_task(client, "Dup B")
    client.post(f"/api/tasks/{b}/dependencies", data={"depends_on_task_id": a})
    r = client.post(f"/api/tasks/{b}/dependencies", data={"depends_on_task_id": a})
    assert r.status_code == 400


def test_self_dependency_prevented(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    a = new_task(client, "Self A")
    r = client.post(f"/api/tasks/{a}/dependencies", data={"depends_on_task_id": a})
    assert r.status_code == 400


def test_cycle_detected_and_rejected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    a = new_task(client, "Cycle A")
    b = new_task(client, "Cycle B")
    c = new_task(client, "Cycle C")
    assert client.post(f"/api/tasks/{b}/dependencies", data={"depends_on_task_id": a}).status_code == 200
    assert client.post(f"/api/tasks/{c}/dependencies", data={"depends_on_task_id": b}).status_code == 200
    r = client.post(f"/api/tasks/{a}/dependencies", data={"depends_on_task_id": c})
    assert r.status_code == 400
    assert "cycle" in r.json()["message"].lower()
    # the graph must be unchanged after a rejected cycle
    deps = client.get(f"/api/tasks/{a}/dependencies").json()["depends_on"]
    assert deps == []


def test_cross_change_dependency_rejected_same_change_allowed(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid1 = new_change(client, "Change one")
    cid2 = new_change(client, "Change two")
    a = new_task(client, "Scoped A")
    b = new_task(client, "Scoped B")
    client.app.state.changes.attach_task_to_change(cid1, a)
    client.app.state.changes.attach_task_to_change(cid2, b)
    r = client.post(f"/api/tasks/{b}/dependencies", data={"depends_on_task_id": a})
    assert r.status_code == 400

    c = new_task(client, "Scoped C")
    client.app.state.changes.attach_task_to_change(cid1, c)
    r2 = client.post(f"/api/tasks/{c}/dependencies", data={"depends_on_task_id": a})
    assert r2.status_code == 200


def test_readiness_ready_task_appears_in_change_ready_tasks(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Ready tasks change")
    create_workflow(client, cid, "VIBE")
    tid = new_task(client, "Ready task")
    client.app.state.changes.attach_task_to_change(cid, tid)
    body = client.get(f"/api/changes/{cid}/ready-tasks").json()
    assert tid in body["ready_tasks"]


# ================================================================== Gates

def test_spec_gate_reused_never_reimplemented(client, git_repo):
    """SPEC_APPROVED gate is satisfied by a real, approved FEATURE_SPEC
    WorkProduct -- reusing WorkProductService (E1), never a parallel
    calculation."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Spec-gated change", project_id=rid)
    create_workflow(client, cid, "AGENTIC_STANDARD")

    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "SPEC_APPROVED" in state["unmet_gates"]

    client.app.state.work_products.create(kind="FEATURE_SPEC", title="Approved spec", change_id=cid, status="APPROVED")
    state2 = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "SPEC_APPROVED" not in state2["unmet_gates"]


def test_spec_compliance_gate_reuses_spec_compliance_verifier(client, git_repo):
    """SPEC_COMPLIANCE_PASS must never PASS on a Change with zero
    spec-linked Tasks (missing evidence never becomes PASS)."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Compliance change", project_id=rid)
    create_workflow(client, cid, "AGENTIC_STANDARD")
    tid = new_task(client, "Unlinked task")
    client.app.state.changes.attach_task_to_change(cid, tid)
    full_review_pass(client, tid, rid)

    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "SPEC_COMPLIANCE_PASS" in state["unmet_gates"]  # no spec linkage at all -> never silently PASS


def test_review_pass_gate_reuses_task_decision_service(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Review gate change", project_id=rid)
    create_workflow(client, cid, "AGENTIC_STANDARD")
    tid = new_task(client, "Reviewed task")
    client.app.state.changes.attach_task_to_change(cid, tid)

    state_before = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "REVIEW_PASS" in state_before["unmet_gates"]

    full_review_pass(client, tid, rid)
    state_after = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "REVIEW_PASS" not in state_after["unmet_gates"]


def test_tests_pass_gate_never_becomes_true_without_real_test_evidence(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Tests gate change", project_id=rid)
    create_workflow(client, cid, "VIBE")
    tid = new_task(client, "Untested task")
    client.app.state.changes.attach_task_to_change(cid, tid)
    full_review_pass(client, tid, rid)

    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "TESTS_PASS" in state["unmet_gates"]


def test_deploy_verified_gate_reuses_deployments_table(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Deploy gate change", project_id=rid)
    tid = new_task(client, "Deploy task")
    client.app.state.changes.attach_task_to_change(cid, tid)
    db = client.app.state.db
    did = db.execute(
        "INSERT INTO deployments(task_id,repository_id,environment,target_name,source_branch,source_commit,status) VALUES(?,?,?,?,?,?,?)",
        (tid, rid, "DEV", "t", "main", "abc123", "PENDING"))
    assert client.app.state.workflow_service._gate_deploy_verified(cid, [dict(id=tid)]) is False
    db.execute("UPDATE deployments SET status='VERIFIED' WHERE id=?", (did,))
    db.execute("INSERT INTO merge_records(task_id,repository_id,required,merge_status) VALUES(?,?,1,'MERGED')", (tid, rid))
    assert client.app.state.workflow_service._gate_deploy_verified(cid, [dict(id=tid)]) is True


def test_deployment_requested_condition_makes_deploy_stage_required(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Deploy required_if change", project_id=rid)
    create_workflow(client, cid, "VIBE")
    tid = new_task(client, "Deploy-requested task")
    client.app.state.changes.attach_task_to_change(cid, tid)

    state_before = client.get(f"/api/changes/{cid}/workflow/state").json()
    deploy_stage = next(s for s in state_before["stages"] if s["stage"] == "DEPLOY")
    assert deploy_stage["requirement"] == "NOT_APPLICABLE"

    db = client.app.state.db
    db.execute(
        "INSERT INTO deployments(task_id,repository_id,environment,target_name,source_branch,source_commit,status) VALUES(?,?,?,?,?,?,?)",
        (tid, rid, "DEV", "t", "main", "abc123", "PENDING"))
    state_after = client.get(f"/api/changes/{cid}/workflow/state").json()
    deploy_stage_after = next(s for s in state_after["stages"] if s["stage"] == "DEPLOY")
    assert deploy_stage_after["requirement"] == "REQUIRED"
    assert "DEPLOY_VERIFIED" in state_after["unmet_gates"]


# ============================================================= State

def test_pending_when_no_tasks_attached(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Empty change", project_id=rid)
    create_workflow(client, cid, "VIBE")
    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state["status"] == "PENDING"


def test_active_once_a_task_exists_and_progresses(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Active change", project_id=rid)
    create_workflow(client, cid, "VIBE")
    tid = new_task(client, "Active task")
    client.app.state.changes.attach_task_to_change(cid, tid)
    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state["status"] == "ACTIVE"
    assert state["current_stage"] == "VERIFY"  # BUILD complete (task exists), VERIFY unmet


def test_blocked_when_a_task_is_blocked(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Blocked change", project_id=rid)
    create_workflow(client, cid, "VIBE")
    tid = new_task(client, "Will be blocked")
    client.app.state.changes.attach_task_to_change(cid, tid)
    setup_fast_ready_launcher(client, "codex")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="codex")
    client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "codex"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "BLOCKED"})

    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state["status"] == "BLOCKED"


def test_waiting_human_when_only_human_acceptance_remains(client, git_repo):
    """Builds every CONTROLLED gate with real evidence except
    HUMAN_ACCEPTANCE, to prove WAITING_HUMAN (and then COMPLETE) are
    reached through the real evaluator -- no stubbing/monkeypatching."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Human acceptance change", project_id=rid)
    create_workflow(client, cid, "CONTROLLED")
    tid = new_task(client, "Controlled task", risk="LOW")
    client.app.state.changes.attach_task_to_change(cid, tid)

    # SPEC_APPROVED, DESIGN_READY
    client.app.state.work_products.create(kind="FEATURE_SPEC", title="Spec", change_id=cid, status="APPROVED")
    client.app.state.work_products.create(kind="TECHNICAL_DESIGN", title="Design", change_id=cid, status="APPROVED")

    # Link the Task to the Spec Layer's own real, already-approved
    # feature (SPEC_COMPLIANCE_PASS reuses SpecComplianceVerifier for real).
    client.post(f"/api/tasks/{tid}/spec", data={
        "classification": "BUG_FIX_TO_EXISTING_SPEC", "feature_id": "FEAT-SPEC-LAYER",
        "requirement_ids": "REQ-001", "acceptance_ids": "AC-001",
    })

    # REVIEW_PASS + SECURITY_PASS (same evidence)
    w = full_review_pass(client, tid, rid)

    # TESTS_PASS: a real test_runs PASS row pinned to the exact submitted HEAD.
    head = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]["last_commit"]
    client.app.state.db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit) VALUES('agent',?,?,?,?,?)",
        (w["id"], "pytest", "test", "PASS", head))

    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state["unmet_gates"] == ["HUMAN_ACCEPTANCE"], state["unmet_gates"]
    assert state["current_stage"] == "HUMAN_ACCEPTANCE"
    assert state["status"] == "WAITING_HUMAN"

    # E11: HUMAN_ACCEPTANCE is now satisfied by a real, ACCEPTED
    # ProductAcceptance bound to the exact current production Release/
    # artifact -- a generic approved HUMAN_DECISION WorkProduct (the
    # pre-E11 placeholder) no longer satisfies it, by design (E11.13:
    # "never a default PASS merely because production is healthy").
    # Minimal-but-real production evidence: a VERIFIED PRODUCTION
    # deployment + a PRODUCTION_VERIFIED Release referencing it, linked
    # to this Change's own Task via release_tasks (the exact join
    # ProductAcceptanceService._current_release() uses).
    db = client.app.state.db
    dep_id = db.execute(
        "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,status,artifact_version,artifact_digest) "
        "VALUES(?,?,?,?,?,?,?)", (rid, "PRODUCTION", "main", head, "VERIFIED", "v1", "sha256:testdigest"))
    release_id = db.execute(
        "INSERT INTO releases(repository_id,version,source_commit,status,production_deployment_id,artifact_digest,artifact_version) "
        "VALUES(?,?,?,?,?,?,?)", (rid, "v1", head, "PRODUCTION_VERIFIED", dep_id, "sha256:testdigest", "v1"))
    db.execute("INSERT INTO release_tasks(release_id,task_id,merged_commit) VALUES(?,?,?)", (release_id, tid, head))

    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")
    accepted = pas.accept(pa["id"], "human", "Looks good")
    assert accepted["status"] == "ACCEPTED"

    state2 = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state2["status"] == "COMPLETE"
    assert state2["current_stage"] is None
    assert state2["unmet_gates"] == []


def test_complete_only_when_every_required_gate_is_met(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Complete VIBE change", project_id=rid)
    create_workflow(client, cid, "VIBE")
    tid = new_task(client, "Complete task")
    client.app.state.changes.attach_task_to_change(cid, tid)
    full_review_pass(client, tid, rid)
    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state["status"] == "ACTIVE"  # TESTS_PASS still unmet -- never fake COMPLETE
    assert "TESTS_PASS" in state["unmet_gates"]


# ================================================== Backward compatibility

def test_existing_task_launch_unaffected_by_workflow_engine(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "codex")
    tid = new_task(client, "Plain task, no workflow")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="codex")
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303


def test_legacy_task_no_change_no_type_no_workflow(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Fully legacy task")
    t = client.get(f"/api/tasks/{tid}").json()
    assert t["change_id"] is None
    assert t["task_type"] is None
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "BACKLOG"


def test_existing_change_behavior_unaffected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    cid = new_change(client, "Change without workflow")
    tid = new_task(client, "Task for changeless-workflow change")
    client.app.state.changes.attach_task_to_change(cid, tid)
    tasks = client.app.state.changes.list_tasks_for_change(cid)
    assert [t["id"] for t in tasks] == [tid]
    assert client.get(f"/api/changes/{cid}/workflow").status_code == 404


def test_spec_gate_unchanged(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Spec gate unaffected task")
    gate = client.get(f"/api/tasks/{tid}/spec-gate").json()
    assert gate["outcome"] == "NOT_APPLICABLE"


def test_task_decision_service_unaffected_by_dependencies(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    a = new_task(client, "TDS unaffected A")
    b = new_task(client, "TDS unaffected B")
    before = client.get(f"/api/tasks/{b}/decision").json()
    client.post(f"/api/tasks/{b}/dependencies", data={"depends_on_task_id": a})
    after = client.get(f"/api/tasks/{b}/decision").json()
    assert before["status"] == after["status"] == "BACKLOG"


def test_engineering_role_validation_unaffected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    r = client.post("/api/engineering/validate-assignment", data={"provider": "codex", "role_key": "BUILDER"})
    assert r.json()["valid"] is True
