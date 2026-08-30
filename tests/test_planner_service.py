"""Dynamic Planner (Phase E4). Every test here injects a fake
PlannerAgentInvoker.runner (same DI pattern as DeploymentService/
GitHubMergeService tests) so the full pipeline runs deterministically
without a real subprocess -- EXCEPT test_workflow_engine... no, except
the one dedicated real-invocation test in this file
(test_real_planner_invocation_end_to_end), which is the E4.21 "real
Planner test" and is the only place a real `claude -p` subprocess is
ever spawned in this suite."""
from __future__ import annotations
import json
import shutil

import pytest

from app.services.planner_service import PlannerAgentError


def register(client, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    return client.get("/api/repositories").json()[0]["id"]


def new_change(client, title, project_id=None):
    data = {"title": title}
    if project_id is not None:
        data["project_id"] = str(project_id)
    return client.post("/api/changes", data=data).json()["id"]


def new_task(client, title, risk="LOW"):
    r = client.post("/api/tasks", data={"title": title, "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303
    return [t["id"] for t in client.get("/api/tasks").json() if t["title"] == title][0]


def fake_envelope(plan_dict):
    return {"is_error": False, "subtype": "success", "result": json.dumps(plan_dict)}


def set_fake_plan(client, plan_dict):
    envelope = fake_envelope(plan_dict)

    def fake_runner(argv, cwd, timeout):
        class R:
            returncode = 0
            stdout = json.dumps(envelope)
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = fake_runner


def minimal_valid_plan(**overrides):
    plan = {"summary": "A minimal, valid plan.", "assumptions": [], "human_decisions": [],
            "tasks": [{"key": "T1", "title": "Implement it", "task_type": "IMPLEMENTATION",
                       "preferred_role": "BUILDER", "depends_on": [], "rationale": "core work"}]}
    plan.update(overrides)
    return plan


def create_and_plan(client, git_repo, profile="VIBE", plan=None, provider="claude"):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Planner test change", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": profile})
    set_fake_plan(client, plan or minimal_valid_plan())
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": provider})
    return cid, r


# =================================================================== Plan domain

def test_plan_create_and_revision_numbering(client, git_repo):
    cid, r = create_and_plan(client, git_repo)
    assert r.status_code == 200
    plan1 = r.json()["plan"]
    assert plan1["revision"] == 1
    assert plan1["change_id"] == cid

    set_fake_plan(client, minimal_valid_plan(summary="A second plan"))
    r2 = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    plan2 = r2.json()["plan"]
    assert plan2["revision"] == 2
    assert plan2["supersedes_plan_id"] is None  # plan_change (not replan) never sets it


def test_replan_sets_supersedes_and_marks_old_plan_superseded(client, git_repo):
    cid, r = create_and_plan(client, git_repo)
    plan1_id = r.json()["plan"]["id"]

    set_fake_plan(client, minimal_valid_plan(summary="Revised plan"))
    r2 = client.post(f"/api/changes/{cid}/replan", data={"provider": "claude"})
    plan2 = r2.json()["plan"]
    assert plan2["supersedes_plan_id"] == plan1_id
    assert plan2["revision"] == 2

    old = client.get(f"/api/plans/{plan1_id}").json()
    assert old["status"] == "SUPERSEDED"


def test_materialized_plan_never_marked_superseded_by_replan(client, git_repo):
    """History-friendly (E4.1/E4.11): a MATERIALIZED plan's status is
    never rewritten -- only the still-open predecessor is superseded."""
    cid, r = create_and_plan(client, git_repo)
    plan1_id = r.json()["plan"]["id"]
    client.post(f"/api/plans/{plan1_id}/materialize")
    assert client.get(f"/api/plans/{plan1_id}").json()["status"] == "MATERIALIZED"

    set_fake_plan(client, minimal_valid_plan(summary="v2"))
    client.post(f"/api/changes/{cid}/replan", data={"provider": "claude"})
    still = client.get(f"/api/plans/{plan1_id}").json()
    assert still["status"] == "MATERIALIZED"  # never SUPERSEDED


def test_plan_list_ordered_by_revision(client, git_repo):
    cid, r = create_and_plan(client, git_repo)
    set_fake_plan(client, minimal_valid_plan(summary="v2"))
    client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    plans = client.get(f"/api/changes/{cid}/plans").json()
    assert [p["revision"] for p in plans] == [1, 2]


def test_plan_get_404_for_unknown_id(client):
    assert client.get("/api/plans/999999").status_code == 404


# ============================================================= Planner context

def test_context_includes_change_workflow_spec_roles_tasktypes_progress(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Context test change", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
    tid = new_task(client, "Existing task for context")
    client.app.state.changes.attach_task_to_change(cid, tid)
    client.app.state.work_products.create(kind="FEATURE_SPEC", title="Existing spec wp", change_id=cid, status="APPROVED")

    ctx = client.app.state.planner_service.context_builder.build(cid, "AGENTIC_STANDARD")
    assert ctx["change"]["id"] == cid
    assert ctx["change"]["title"] == "Context test change"
    assert "SPEC" in ctx["workflow"]["required_stages"]
    assert ctx["spec"]["baseline_sha256"] and len(ctx["spec"]["baseline_sha256"]) == 64
    assert any(f["id"] == "FEAT-SPEC-LAYER" for f in ctx["spec"]["approved_features"])
    assert "BUILDER" in ctx["engineering"]["roles"]
    assert "IMPLEMENTATION" in ctx["engineering"]["task_types"]
    assert any(t["id"] == tid for t in ctx["existing_progress"]["tasks"])
    assert any(wp["kind"] == "FEATURE_SPEC" for wp in ctx["existing_progress"]["work_products"])


def test_context_digest_is_deterministic(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Digest test change", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})
    from app.services.planner_service import PlannerContextBuilder
    ctx1 = client.app.state.planner_service.context_builder.build(cid, "VIBE")
    ctx2 = client.app.state.planner_service.context_builder.build(cid, "VIBE")
    assert PlannerContextBuilder.digest(ctx1) == PlannerContextBuilder.digest(ctx2)


# ============================================================= Structured output

def test_valid_output_produces_plan_ready(client, git_repo):
    cid, r = create_and_plan(client, git_repo, profile="VIBE")
    assert r.json()["outcome"] == "PLAN_READY"
    assert r.json()["plan"]["status"] == "VALIDATED"


def test_malformed_envelope_produces_execution_failed_no_plan_row(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Bad envelope change", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})

    def bad_runner(argv, cwd, timeout):
        class R:
            returncode = 0
            stdout = "not json at all {{{"
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = bad_runner
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    assert r.json()["outcome"] == "PLANNER_EXECUTION_FAILED"
    assert r.json()["plan"] is None
    assert client.get(f"/api/changes/{cid}/plans").json() == []


def test_malformed_inner_result_produces_output_invalid_no_plan_row(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Bad inner result change", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})
    envelope = {"is_error": False, "subtype": "success", "result": "just prose, not JSON"}

    def runner(argv, cwd, timeout):
        class R:
            returncode = 0
            stdout = json.dumps(envelope)
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = runner
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    assert r.json()["outcome"] == "PLANNER_OUTPUT_INVALID"
    assert client.get(f"/api/changes/{cid}/plans").json() == []


def test_unknown_task_type_rejected(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(
        tasks=[{"key": "T1", "title": "x", "task_type": "NOT_A_REAL_TYPE"}]))
    assert r.json()["outcome"] == "PLAN_INVALID"
    assert any("unknown task_type" in e for e in r.json()["validation"]["errors"])
    assert r.json()["plan"]["status"] == "REJECTED"


def test_unknown_role_rejected(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(
        tasks=[{"key": "T1", "title": "x", "task_type": "IMPLEMENTATION", "preferred_role": "NOT_A_ROLE"}]))
    assert r.json()["outcome"] == "PLAN_INVALID"
    assert any("unknown preferred_role" in e for e in r.json()["validation"]["errors"])


def test_incompatible_role_is_warning_not_error(client, git_repo):
    """E4.9: preserve the preferred role, report a warning -- never
    silently substitute, never hard-reject a known-but-mismatched role."""
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(
        tasks=[{"key": "T1", "title": "x", "task_type": "IMPLEMENTATION", "preferred_role": "RELEASE_MANAGER"}]))
    assert r.json()["outcome"] == "PLAN_READY"
    assert any("not associated with task_type" in w for w in r.json()["validation"]["warnings"])
    items = client.get(f"/api/plans/{r.json()['plan']['id']}/task-graph").json()
    assert items[0]["preferred_role"] == "RELEASE_MANAGER"  # preserved verbatim


# ================================================================== Dependencies

def test_valid_dependency_graph_accepted(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(tasks=[
        {"key": "T1", "title": "Design", "task_type": "TECHNICAL_DESIGN"},
        {"key": "T2", "title": "Build", "task_type": "IMPLEMENTATION", "depends_on": ["T1"]},
    ]))
    assert r.json()["outcome"] == "PLAN_READY"


def test_self_dependency_rejected(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(
        tasks=[{"key": "T1", "title": "x", "task_type": "IMPLEMENTATION", "depends_on": ["T1"]}]))
    assert r.json()["outcome"] == "PLAN_INVALID"
    assert any("cannot depend on itself" in e for e in r.json()["validation"]["errors"])


def test_unknown_dependency_key_rejected(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(
        tasks=[{"key": "T1", "title": "x", "task_type": "IMPLEMENTATION", "depends_on": ["GHOST"]}]))
    assert r.json()["outcome"] == "PLAN_INVALID"
    assert any("unknown key" in e for e in r.json()["validation"]["errors"])


def test_dependency_cycle_rejected(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(tasks=[
        {"key": "A", "title": "A", "task_type": "IMPLEMENTATION", "depends_on": ["B"]},
        {"key": "B", "title": "B", "task_type": "IMPLEMENTATION", "depends_on": ["A"]},
    ]))
    assert r.json()["outcome"] == "PLAN_INVALID"
    assert any("cycle" in e.lower() for e in r.json()["validation"]["errors"])


def test_duplicate_task_key_rejected(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(tasks=[
        {"key": "T1", "title": "A", "task_type": "IMPLEMENTATION"},
        {"key": "T1", "title": "B", "task_type": "IMPLEMENTATION"},
    ]))
    assert r.json()["outcome"] == "PLAN_INVALID"
    assert any("Duplicate task key" in e for e in r.json()["validation"]["errors"])


# ============================================================== Workflow coverage

def test_vibe_accepts_minimal_build_only_plan(client, git_repo):
    cid, r = create_and_plan(client, git_repo, profile="VIBE", plan=minimal_valid_plan(
        tasks=[{"key": "T1", "title": "Build", "task_type": "IMPLEMENTATION"}]))
    assert r.json()["outcome"] == "PLAN_READY"


def test_agentic_standard_missing_review_coverage_rejected(client, git_repo):
    """AGENTIC_STANDARD requires SPEC+BUILD+REVIEW+VERIFY -- a plan with
    only a SPEC task never reaches BUILD (which implicitly covers
    REVIEW/VERIFY), so it must be rejected for missing coverage."""
    cid, r = create_and_plan(client, git_repo, profile="AGENTIC_STANDARD", plan=minimal_valid_plan(
        tasks=[{"key": "T1", "title": "Spec only", "task_type": "SPEC_AUTHORING"}]))
    assert r.json()["outcome"] == "PLAN_INVALID"
    errs = " ".join(r.json()["validation"]["errors"])
    assert "BUILD" in errs


def test_agentic_standard_covers_review_verify_implicitly_via_build(client, git_repo):
    cid, r = create_and_plan(client, git_repo, profile="AGENTIC_STANDARD", plan=minimal_valid_plan(tasks=[
        {"key": "T1", "title": "Spec", "task_type": "SPEC_AUTHORING"},
        {"key": "T2", "title": "Build", "task_type": "IMPLEMENTATION", "depends_on": ["T1"]},
    ]))
    assert r.json()["outcome"] == "PLAN_READY"
    cov = r.json()["validation"]["stage_coverage"]
    assert cov["REVIEW"] is True and cov["VERIFY"] is True


def test_controlled_cannot_skip_design(client, git_repo):
    cid, r = create_and_plan(client, git_repo, profile="CONTROLLED", plan=minimal_valid_plan(tasks=[
        {"key": "T1", "title": "Spec", "task_type": "SPEC_AUTHORING"},
        {"key": "T2", "title": "Build", "task_type": "IMPLEMENTATION", "depends_on": ["T1"]},
        {"key": "T3", "title": "Release", "task_type": "RELEASE", "depends_on": ["T2"]},
    ]))
    assert r.json()["outcome"] == "PLAN_INVALID"
    assert any("DESIGN" in e for e in r.json()["validation"]["errors"])


def test_controlled_human_acceptance_is_coverage_exempt_but_still_a_real_gate(client, git_repo):
    """HUMAN_ACCEPTANCE is never something a Plan task 'covers' -- but
    the underlying gate still exists and is unmet until a real human
    decision WorkProduct is recorded (proven via workflow state, not
    plan validation)."""
    cid, r = create_and_plan(client, git_repo, profile="CONTROLLED", plan=minimal_valid_plan(tasks=[
        {"key": "T1", "title": "Spec", "task_type": "SPEC_AUTHORING"},
        {"key": "T2", "title": "Design", "task_type": "TECHNICAL_DESIGN", "depends_on": ["T1"]},
        {"key": "T3", "title": "Build", "task_type": "IMPLEMENTATION", "depends_on": ["T2"]},
        {"key": "T4", "title": "Release", "task_type": "RELEASE", "depends_on": ["T3"]},
    ]))
    assert r.json()["outcome"] == "PLAN_READY"
    assert r.json()["validation"]["stage_coverage"]["HUMAN_ACCEPTANCE"] is True
    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "HUMAN_ACCEPTANCE" in state["unmet_gates"]


def test_project_policy_still_governs_profile_choice_for_planning(client, git_repo):
    root, repo = git_repo
    (repo / "PROJECT.yaml").write_text(
        (repo / "PROJECT.yaml").read_text() + "\nengineering:\n  workflow:\n    allowed_profiles: [VIBE]\n")
    import subprocess
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "policy"], cwd=repo, check=True)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Policy change", project_id=rid)
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "CONTROLLED"})
    assert r.status_code == 400  # workflow creation itself already enforces this (E3), unchanged by E4


# ================================================================= Role validation

def test_planner_assignment_valid_for_claude(client, git_repo):
    cid, r = create_and_plan(client, git_repo, provider="claude")
    assert r.json()["outcome"] not in ("PLANNER_ASSIGNMENT_INVALID",)


def test_planner_assignment_invalid_for_unsupported_provider(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Bad provider change", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": "gemini"})
    assert r.json()["outcome"] == "PLANNER_ASSIGNMENT_INVALID"
    assert client.get(f"/api/changes/{cid}/plans").json() == []


# =================================================================== Materialization

def test_materialize_creates_real_tasks_with_type_and_change(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(tasks=[
        {"key": "T1", "title": "Design it", "task_type": "TECHNICAL_DESIGN"},
        {"key": "T2", "title": "Build it", "task_type": "IMPLEMENTATION", "depends_on": ["T1"]},
    ]))
    plan_id = r.json()["plan"]["id"]
    m = client.post(f"/api/plans/{plan_id}/materialize")
    assert m.status_code == 200
    task_ids = m.json()["task_ids"]
    assert set(task_ids) == {"T1", "T2"}

    t2 = client.get(f"/api/tasks/{task_ids['T2']}").json()
    assert t2["task_type"] == "IMPLEMENTATION"
    assert t2["change_id"] == cid
    assert t2["status"] == "BACKLOG"

    deps = client.get(f"/api/tasks/{task_ids['T2']}/dependencies").json()["depends_on"]
    assert [d["depends_on_task_id"] for d in deps] == [task_ids["T1"]]


def test_materialize_never_starts_any_agent_session(client, git_repo):
    cid, r = create_and_plan(client, git_repo)
    plan_id = r.json()["plan"]["id"]
    m = client.post(f"/api/plans/{plan_id}/materialize")
    task_id = list(m.json()["task_ids"].values())[0]
    sessions = client.app.state.db.all("SELECT * FROM agent_sessions WHERE task_id=?", (task_id,))
    assert sessions == []


def test_cannot_materialize_a_draft_or_rejected_plan(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(
        tasks=[{"key": "T1", "title": "x", "task_type": "NOT_A_TYPE"}]))
    assert r.json()["plan"]["status"] == "REJECTED"
    m = client.post(f"/api/plans/{r.json()['plan']['id']}/materialize")
    assert m.status_code == 400


def test_materialize_is_atomic_on_partial_failure(client, git_repo):
    """A corrupted plan_items row (simulating an unexpected mid-loop
    failure) must never leave a partially-created task graph behind --
    the whole materialize_plan() call is one DB transaction (E4.8)."""
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(tasks=[
        {"key": "T1", "title": "A", "task_type": "IMPLEMENTATION"},
        {"key": "T2", "title": "B", "task_type": "IMPLEMENTATION", "depends_on": ["T1"]},
    ]))
    plan_id = r.json()["plan"]["id"]
    db = client.app.state.db
    tasks_before = len(db.all("SELECT id FROM tasks"))

    # Corrupt T2's dependency data so the second phase of the
    # transaction (dependency resolution, which runs AFTER both tasks
    # have already been INSERTed but before commit) raises.
    db.execute("UPDATE plan_items SET depends_on_keys=? WHERE plan_id=? AND item_key='T2'", ("{not valid json", plan_id))

    svc = client.app.state.planner_service
    with pytest.raises(Exception):
        svc.materialize_plan(plan_id)

    # Nothing partially materialized: plan status unchanged, no stray
    # tasks, T1's own INSERT (which ran successfully before the crash)
    # was rolled back along with everything else in the transaction.
    plan_after = svc.get_plan(plan_id)
    assert plan_after["status"] == "VALIDATED"
    items = svc.plan_items(plan_id)
    assert all(it["materialized_task_id"] is None for it in items)
    assert len(db.all("SELECT id FROM tasks")) == tasks_before


def test_materialize_recognizes_existing_tasks_via_explicit_linkage_not_dedup(client, git_repo):
    """E4.17: no semantic deduplication -- an existing Task for the
    Change is simply untouched by materialization; the Plan's own
    materialized_task_id linkage is the only explicit tracking."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Existing tasks change", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})
    existing_tid = new_task(client, "Pre-existing manual task")
    client.app.state.changes.attach_task_to_change(cid, existing_tid)

    set_fake_plan(client, minimal_valid_plan())
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    plan_id = r.json()["plan"]["id"]
    m = client.post(f"/api/plans/{plan_id}/materialize")
    new_tid = list(m.json()["task_ids"].values())[0]
    assert new_tid != existing_tid
    tasks = client.get(f"/api/changes/{cid}/tasks").json()
    assert {t["id"] for t in tasks} == {existing_tid, new_tid}


# ===================================================================== Human decisions

def test_unresolved_human_decision_blocks_materialization(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(
        human_decisions=[{"question": "Delete old data?", "reason": "destructive", "spec_change_signal": "HUMAN_SPEC_CHANGE_REQUIRED"}]))
    assert r.json()["outcome"] == "HUMAN_DECISION_REQUIRED"
    plan_id = r.json()["plan"]["id"]
    assert plan_id  # structurally VALIDATED
    assert r.json()["plan"]["status"] == "VALIDATED"
    m = client.post(f"/api/plans/{plan_id}/materialize")
    assert m.status_code == 400
    assert "unresolved human decisions" in m.json()["message"]


def test_workflow_waiting_human_while_decision_unresolved(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(
        human_decisions=[{"question": "Change permission model?", "reason": "security boundary"}]))
    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state["status"] == "WAITING_HUMAN"


def test_resolving_decision_unblocks_materialization(client, git_repo):
    cid, r = create_and_plan(client, git_repo, plan=minimal_valid_plan(
        human_decisions=[{"question": "Proceed with X?", "reason": "ambiguous requirement"}]))
    plan_id = r.json()["plan"]["id"]
    hd = client.get(f"/api/plans/{plan_id}").json()["human_decisions"][0]
    resolve = client.post(f"/api/plans/{plan_id}/human-decisions/{hd['id']}/resolve", data={"resolution_note": "Yes, proceed as-is."})
    assert resolve.status_code == 200
    assert resolve.json()["resolved"] == 1

    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state["status"] != "WAITING_HUMAN"

    m = client.post(f"/api/plans/{plan_id}/materialize")
    assert m.status_code == 200


# ==================================================================== Backward compat

def test_manual_task_creation_path_unaffected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    r = client.post("/api/tasks", data={"title": "Purely manual task", "risk_profile": "LOW"}, follow_redirects=False)
    assert r.status_code == 303


def test_existing_workflow_unaffected_by_planner_module_presence(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "No plan ever change", project_id=rid)
    run = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"}).json()
    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert state["status"] == "PENDING"  # unaffected -- no plan involved at all


def test_supervisor_start_unaffected_by_planner_module(client, git_repo):
    from app.launchers import AgentLauncher
    root, repo = git_repo
    rid = register(client, repo, "demo")
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "echo READY; cat"))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1
    tid = new_task(client, "Supervisor unaffected task")
    client.post(f"/api/tasks/{tid}/select")
    w = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    w = [x for x in client.get(f"/api/tasks/{tid}").json()["workspaces"] if x["agent"] == "codex"][-1]
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303


def test_spec_gate_unaffected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Spec gate unaffected by planner")
    assert client.get(f"/api/tasks/{tid}/spec-gate").json()["outcome"] == "NOT_APPLICABLE"


def test_task_decision_service_unaffected_by_materialized_task(client, git_repo):
    cid, r = create_and_plan(client, git_repo)
    plan_id = r.json()["plan"]["id"]
    m = client.post(f"/api/plans/{plan_id}/materialize")
    task_id = list(m.json()["task_ids"].values())[0]
    d = client.get(f"/api/tasks/{task_id}/decision").json()
    assert d["status"] == "BACKLOG"  # a materialized Task starts exactly like a manually-created one


# ============================================================= Real Planner test (E4.21)

@pytest.mark.skipif(not shutil.which("claude"), reason="real `claude` CLI not available in this environment")
def test_real_planner_invocation_end_to_end(client, git_repo):
    """E4.21: one safe, real, non-fake Planner invocation. Disposable
    Change/workflow, real `claude -p --json-schema` subprocess call, no
    production repository mutation, no Task execution -- materializes
    only into disposable Task records and asserts no Agent session was
    ever started for them."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Add an endpoint returning the current workflow profile", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})

    r = client.post(f"/api/changes/{cid}/plan", data={
        "provider": "claude",
        # note: real invocation uses the REAL invoker (no fake runner
        # installed in this test) -- this is deliberate.
    })
    assert r.status_code == 200, r.text
    body = r.json()
    print("REAL PLANNER TEST -- outcome:", body["outcome"])
    assert body["outcome"] in ("PLAN_READY", "HUMAN_DECISION_REQUIRED"), body

    plan = body["plan"]
    print("REAL PLANNER TEST -- plan id:", plan["id"], "revision:", plan["revision"], "provider:", plan["planner_provider"])
    print("REAL PLANNER TEST -- validation:", json.dumps(body["validation"]))
    items = client.get(f"/api/plans/{plan['id']}/task-graph").json()
    print("REAL PLANNER TEST -- task count:", len(items))
    dep_count = sum(len(it["depends_on"]) for it in items)
    print("REAL PLANNER TEST -- dependency count:", dep_count)
    hds = client.get(f"/api/plans/{plan['id']}").json()["human_decisions"]
    print("REAL PLANNER TEST -- human decisions:", len(hds))
    for hd in hds:
        print("REAL PLANNER TEST -- human decision:", hd["question"], "|", hd["spec_change_signal"])
    assert len(items) >= 1

    if body["outcome"] == "PLAN_READY":
        m = client.post(f"/api/plans/{plan['id']}/materialize")
        assert m.status_code == 200, m.text
        task_ids = list(m.json()["task_ids"].values())
        print("REAL PLANNER TEST -- materialized task ids:", task_ids)
        for tid in task_ids:
            sessions = client.app.state.db.all("SELECT id FROM agent_sessions WHERE task_id=?", (tid,))
            assert sessions == [], "Planner must never start a Task execution"
        print("REAL PLANNER TEST -- confirmed: no Agent session started for any materialized Task")
    else:
        # HUMAN_DECISION_REQUIRED: materialization must never be
        # attempted past an unresolved WHAT-level decision -- confirm
        # zero Tasks exist for this disposable Change at all.
        assert client.get(f"/api/changes/{cid}/tasks").json() == []
        print("REAL PLANNER TEST -- confirmed: zero Tasks materialized/executed (unresolved human decision correctly blocked it)")
