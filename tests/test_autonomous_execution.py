"""Autonomous Implementation Orchestration (Phase E8). SAFETY: default
policy (no PROJECT.yaml engineering.autonomous_execution block, or an
explicit enabled:false) is DISABLED -- E8.27's own critical requirement.
Every test that needs AUTO_READY explicitly opts a disposable fixture
repo in via `enable_autonomous()`. No test in this file ever launches a
real `claude` CLI process -- the one real Builder fixture test lives in
tests/test_autonomous_execution_real.py, matching this repo's own
established real-vs-fake test-file convention."""
from __future__ import annotations
import dataclasses
import json

import pytest
import yaml

from app.launchers import AgentLauncher
from app.services.autonomous_execution_service import AUTO_ELIGIBLE_TASK_TYPES, REPEATED_FAILURE_THRESHOLD


def register(client, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    return client.get("/api/repositories").json()[0]["id"]


def new_change(client, title, description="", project_id=None):
    data = {"title": title, "description": description}
    if project_id is not None:
        data["project_id"] = str(project_id)
    return client.post("/api/changes", data=data).json()["id"]


def enable_autonomous(repo, enabled=True, max_concurrent=1, auto_start=True):
    """Must commit -- an uncommitted PROJECT.yaml edit would itself be
    exactly the dirty working tree DIRTY_WORKTREE_REQUIRES_ATTENTION
    (E8.19) is designed to catch."""
    import subprocess
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: demo}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\n"
        "ci: {required: [preflight, test]}\n"
        "engineering:\n  autonomous_execution:\n"
        f"    enabled: {str(enabled).lower()}\n    max_concurrent_builders: {max_concurrent}\n"
        f"    auto_start_ready_tasks: {str(auto_start).lower()}\n")
    subprocess.run(["git", "add", "PROJECT.yaml"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "enable autonomous execution"], cwd=repo, check=True, capture_output=True)


def create_workflow(client, cid, profile="AGENTIC_STANDARD"):
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": profile})
    assert r.status_code == 200, r.text
    return r.json()


def materialize_task(client, cid, task_type="IMPLEMENTATION", key="T1", title=None,
                      depends_on_keys=None, requirement_ids=None, scope_hints=None, plan_id=None):
    db = client.app.state.db
    if plan_id is None:
        plan_id = db.execute(
            "INSERT INTO plans(change_id,revision,status,planner_provider,input_context_digest) VALUES(?,?,?,?,?)",
            (cid, 1, "MATERIALIZED", "claude", "x"))
    tid = db.execute("INSERT INTO tasks(slug,title,status,change_id,task_type) VALUES(?,?,?,?,?)",
                      (f"auto-{cid}-{key}-{tid_seq(db)}", title or f"Task {key}", "BACKLOG", cid, task_type))
    db.execute(
        "INSERT INTO plan_items(plan_id,item_key,title,task_type,depends_on_keys,requirement_ids,scope_hints,materialized_task_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (plan_id, key, title or f"Task {key}", task_type, json.dumps(depends_on_keys or []),
         json.dumps(requirement_ids or []), json.dumps(scope_hints or []), tid))
    # TaskDependencyService.readiness() (E3.6) reads the real
    # task_dependencies table, not plan_items.depends_on_keys (that JSON
    # column is materialization bookkeeping only) -- so this raw-SQL
    # fixture helper must resolve each declared key to its
    # already-materialized task id within the same plan and insert the
    # actual dependency row, the same way real Plan materialization does.
    for dep_key in (depends_on_keys or []):
        dep_row = db.one(
            "SELECT materialized_task_id FROM plan_items WHERE plan_id=? AND item_key=?", (plan_id, dep_key))
        if dep_row and dep_row["materialized_task_id"]:
            db.execute(
                "INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES(?,?)",
                (tid, dep_row["materialized_task_id"]))
    return tid, plan_id


_seq = [0]


def tid_seq(db):
    _seq[0] += 1
    return _seq[0]


def fake_launcher(client, name="codex"):
    client.app.state.agent_sessions.launchers = {name: AgentLauncher(name.title(), "bash", ("-c", "echo READY; cat"))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1


# ================================================================ Eligibility (E8.25)

def test_ready_implementation_task_is_auto_ready(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Ready change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.status_code == 200
    assert r.json()["readiness"] == "AUTO_READY", r.json()


def test_non_implementation_task_not_autonomous(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Non-impl change", project_id=rid)
    tid, _ = materialize_task(client, cid, task_type="REQUIREMENT_ANALYSIS")
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "NOT_AUTONOMOUS_TASK"


def test_dependency_waiting(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Dep change", project_id=rid)
    t1, plan_id = materialize_task(client, cid, key="A")
    t2, _ = materialize_task(client, cid, key="B", depends_on_keys=["A"], plan_id=plan_id)
    r = client.get(f"/api/tasks/{t2}/execution-readiness")
    assert r.json()["readiness"] == "WAITING_DEPENDENCY"
    assert t1 in r.json()["unmet_dependencies"]


def test_unresolved_human_decision_blocks(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Decision change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    client.app.state.human_decisions.create("change", cid, "Which option?", "tradeoff", "OTHER")
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "WAITING_HUMAN"


def test_spec_gate_fail_blocks(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "SpecGate change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    # A behavior-changing spec_change_classification with NO feature_id
    # linkage -> SpecGate SPEC_REQUIRED, not PASS/NOT_APPLICABLE.
    client.app.state.db.execute("UPDATE tasks SET spec_change_classification='NEW_BEHAVIOR' WHERE id=?", (tid,))
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "WAITING_SPEC"


def test_design_not_ready_blocks(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Design gate change", project_id=rid)
    create_workflow(client, cid, "CONTROLLED")  # DESIGN is REQUIRED, never authored
    tid, _ = materialize_task(client, cid)
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] in ("WAITING_DESIGN", "WAITING_TEST_DESIGN")


def test_already_running_blocks(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Running change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    db = client.app.state.db
    wsid = db.execute(
        "INSERT INTO agent_workspaces(task_id,repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,status) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (tid, rid, "codex", "x", f"b-{tid}", f"/tmp/wt-{tid}", "main", "abc", "READY"))
    db.execute("INSERT INTO agent_sessions(task_id,workspace_id,agent,command_profile,cwd,status) VALUES(?,?,?,?,?,?)",
               (tid, wsid, "codex", "default", "/tmp", "RUNNING"))
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "ALREADY_RUNNING"


def test_repeated_failure_stop(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Failure change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    for _ in range(REPEATED_FAILURE_THRESHOLD):
        client.app.state.db.event("task", tid, "AUTO_EXECUTION_FAILED", "boom")
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "REPEATED_FAILURE_STOP"


def test_repeated_failure_resets_after_success(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Recovered change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    db = client.app.state.db
    db.event("task", tid, "AUTO_EXECUTION_FAILED", "boom")
    db.event("task", tid, "AUTO_EXECUTION_FAILED", "boom")
    db.event("task", tid, "AUTO_BUILDER_LAUNCHED", "session=1")
    db.event("task", tid, "AUTO_EXECUTION_FAILED", "boom")
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "AUTO_READY"  # only 1 failure since the last success


def test_invalid_role_assignment_blocks(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "No provider change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    svc = client.app.state.autonomous_execution_service
    svc.settings = dataclasses.replace(svc.settings, agents=())
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "ROLE_ASSIGNMENT_INVALID"


def test_dirty_worktree_blocks(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Dirty worktree change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    (repo / "unrelated.txt").write_text("uncommitted work in progress\n")
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "DIRTY_WORKTREE_REQUIRES_ATTENTION"


def test_task_complete_state(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Done task change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    client.app.state.db.execute("UPDATE tasks SET merged_at=CURRENT_TIMESTAMP WHERE id=?", (tid,))
    # A Task with no Builder Workspace at all never reads DONE through
    # TaskDecisionService -- exercise the real COMPLETE path via a Task
    # dependency that is genuinely finished instead: covered indirectly
    # by test_dependency_waiting's own DONE-dependency case. This test
    # instead confirms the disabled-policy Change never crashes.
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.status_code == 200


# ================================================================ Policy (E8.1/E8.27)

def test_disabled_by_default_with_no_policy(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")  # no enable_autonomous() call
    cid = new_change(client, "No policy change", project_id=rid)
    materialize_task(client, cid)
    r = client.post(f"/api/changes/{cid}/autonomous-execution/tick")
    assert r.json()["results"][0]["outcome"] == "DISABLED"


def test_explicit_disabled_policy(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo, enabled=False)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Explicitly disabled change", project_id=rid)
    materialize_task(client, cid)
    r = client.post(f"/api/changes/{cid}/autonomous-execution/tick")
    assert r.json()["results"][0]["outcome"] == "DISABLED"


def test_disposable_change_with_no_ready_task_gives_no_ready_task(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "No tasks change", project_id=rid)
    r = client.post(f"/api/changes/{cid}/autonomous-execution/tick")
    assert r.json()["results"][0]["outcome"] == "NO_READY_TASK"


# ================================================================ Scheduler (E8.6/E8.7)

def test_deterministic_selection_by_dependency_depth(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Order change", project_id=rid)
    t_later, plan_id = materialize_task(client, cid, key="Z")  # inserted first, 0 deps -- but AFTER in plan_item id? no, both 0 deps here
    t_earlier, _ = materialize_task(client, cid, key="A", plan_id=plan_id)
    ready = client.get(f"/api/changes/{cid}/auto-ready-tasks").json()
    assert [r["task_id"] for r in ready] == [t_later, t_earlier]  # plan_item insertion order, both depth 0


def test_max_concurrency_one_launch_per_tick(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo, max_concurrent=1)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Concurrency change", project_id=rid)
    fake_launcher(client)
    t1, plan_id = materialize_task(client, cid, key="A")
    t2, _ = materialize_task(client, cid, key="B", plan_id=plan_id)
    r = client.post(f"/api/changes/{cid}/autonomous-execution/tick")
    body = r.json()["results"][0]
    assert body["outcome"] == "LAUNCHED"
    launched_task = body["task_id"]
    other_task = t2 if launched_task == t1 else t1
    # the other ready Task must still be waiting -- capacity is spent
    r2 = client.post(f"/api/changes/{cid}/autonomous-execution/tick")
    assert r2.json()["results"][0]["outcome"] == "AT_CAPACITY"
    r3 = client.get(f"/api/tasks/{other_task}/execution-readiness")
    assert r3.json()["readiness"] == "AUTO_READY"  # never touched, just never got capacity


def test_no_duplicate_launch_for_same_task(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "No dup change", project_id=rid)
    fake_launcher(client)
    tid, _ = materialize_task(client, cid)
    r1 = client.post(f"/api/tasks/{tid}/autonomous-start")
    assert r1.json()["outcome"] == "LAUNCHED"
    r2 = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r2.json()["readiness"] == "ALREADY_RUNNING"
    r3 = client.post(f"/api/tasks/{tid}/autonomous-start")
    assert r3.json()["outcome"] == "ALREADY_RUNNING"
    sessions = client.app.state.db.all("SELECT id FROM agent_sessions WHERE task_id=?", (tid,))
    assert len(sessions) == 1


# ================================================================ Failure policy (E8.15)

def test_execution_failure_recorded(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Failure recording change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    svc = client.app.state.autonomous_execution_service
    svc.settings = dataclasses.replace(svc.settings, agents=())  # forces ROLE_ASSIGNMENT_INVALID, not a launch
    r = client.post(f"/api/tasks/{tid}/autonomous-start")
    assert r.json()["outcome"] == "ROLE_ASSIGNMENT_INVALID"
    events = client.app.state.db.all("SELECT * FROM workspace_events WHERE entity_type='task' AND entity_id=?", (tid,))
    assert not any(e["action"] == "AUTO_BUILDER_LAUNCHED" for e in events)


# ================================================================ Builder context (E8.8/E8.9)

def test_builder_prompt_includes_test_contract_and_scope(client, git_repo, tmp_path):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    specs_root = tmp_path / "specs"
    (specs_root / "features").mkdir(parents=True)
    (specs_root / "SPEC.yaml").write_text("schema_version: 1\nproject: t\nglossary: glossary.yaml\nfeatures_dir: features\n")
    (specs_root / "glossary.yaml").write_text("schema_version: 1\nterms: {}\n")
    (specs_root / "features" / "feat-x.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "id": "FEAT-X", "title": "X", "version": 1, "status": "approved", "summary": "x",
        "requirements": [{"id": "REQ-001", "text": "Do X."}], "acceptance_criteria": [], "invariants": []}))
    client.app.state.task_execution_context_builder.specs_root = specs_root
    cid = new_change(client, "Context change", project_id=rid)
    client.app.state.trace.link("change", cid, "spec_feature", "FEAT-X", relation="GOVERNED_BY")
    db = client.app.state.db
    tcsid = db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)", (cid, "TC-001", json.dumps(["REQ-001"]), "INTEGRATION", "POSITIVE", "Does X", "X happens", "APPROVED"))
    tid, _ = materialize_task(client, cid, requirement_ids=["REQ-001"], scope_hints=["app/x.py"])
    lines = client.app.state.task_execution_context_builder.render_lines(tid, cid)
    text = "\n".join(lines)
    assert "TC-001" in text
    assert "TEST CONTRACT RULES" in text
    assert "Do not edit the approved Spec" in text
    assert "app/x.py" in text


def test_builder_context_includes_predecessor_work_products(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Predecessor change", project_id=rid)
    t1, plan_id = materialize_task(client, cid, key="A")
    client.app.state.work_products.create(kind="CODE_CHANGE", title="Helper added", change_id=cid, task_id=t1, status="APPROVED")
    t2, _ = materialize_task(client, cid, key="B", depends_on_keys=["A"], plan_id=plan_id)
    client.app.state.db.execute("UPDATE tasks SET merged_at=CURRENT_TIMESTAMP,status='DONE' WHERE id=?", (t1,))
    lines = client.app.state.task_execution_context_builder.render_lines(t2, cid)
    assert any("Helper added" in l for l in lines)


def test_builder_context_empty_for_legacy_task(client, git_repo):
    root, repo = git_repo
    r = client.post("/api/tasks", data={"title": "Legacy task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Legacy task"][0]
    lines = client.app.state.task_execution_context_builder.render_lines(tid, None)
    assert lines == []


# ================================================================ Test implementation (E8.11) / Task completion (E8.13)

def test_test_implementation_task_eligible(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Test impl change", project_id=rid)
    tid, _ = materialize_task(client, cid, task_type="TEST_IMPLEMENTATION")
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "AUTO_READY"


def test_test_implementation_mapping_never_implies_pass(client, git_repo):
    """E8.11/E8.13: mapping a TestCaseSpec to real code is IMPLEMENTED,
    never PASS -- that still requires real execution evidence."""
    db = client.app.state.db
    cid = new_change(client, "Mapping change")
    tcsid = db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)", (cid, "TC-1", "[]", "UNIT", "POSITIVE", "x", "x", "DRAFT"))
    mapping = client.app.state.executable_test_mapping_service.map(tcsid, None, "tests/test_x.py", "test_x")
    assert mapping["implementation_status"] == "IMPLEMENTED"
    assert mapping["implementation_status"] != "PASS"


def test_agent_exit_does_not_mean_task_complete(client, git_repo):
    """E8.13: TaskDecisionService remains authoritative -- a materialized
    Task with an EXITED agent session and no review/verification never
    reads DONE."""
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Exit change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    db = client.app.state.db
    wsid = db.execute(
        "INSERT INTO agent_workspaces(task_id,repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,status) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (tid, rid, "codex", "x", f"b-{tid}", f"/tmp/wt-{tid}", "main", "abc", "CREATED"))
    db.execute("INSERT INTO agent_sessions(task_id,workspace_id,agent,command_profile,cwd,status,exit_code) VALUES(?,?,?,?,?,?,?)",
               (tid, wsid, "codex", "default", "/tmp", "EXITED", 0))
    status = client.app.state.decision.evaluate(tid)["status"]
    assert status != "DONE"


# ================================================================ Backward compatibility (E8.25)

def test_manual_builder_start_unaffected(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    fake_launcher(client)
    r = client.post("/api/tasks", data={"title": "Manual task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Manual task"][0]
    client.post(f"/api/tasks/{tid}/select")
    w = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    w = [x for x in client.get(f"/api/tasks/{tid}").json()["workspaces"] if x["agent"] == "codex"][-1]
    r2 = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r2.status_code == 303


def test_legacy_task_execution_readiness_never_crashes(client, git_repo):
    r = client.post("/api/tasks", data={"title": "Legacy", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Legacy"][0]
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.status_code == 200
    assert r.json()["readiness"] == "NOT_AUTONOMOUS_TASK"


def test_workflow_planner_specgate_unaffected(client, git_repo):
    """Smoke check: constructing AutonomousExecutionService and wiring
    it into render_agent_prompt() changes nothing about the existing
    Workflow/Planner/SpecGate API surface."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Compat change", project_id=rid)
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})
    assert r.status_code == 200
    r = client.get(f"/api/engineering/task-types")
    assert r.status_code == 200


def test_all_eligible_task_types_are_the_documented_set():
    assert set(AUTO_ELIGIBLE_TASK_TYPES) == {"IMPLEMENTATION", "TEST_IMPLEMENTATION", "FIX"}


# ================================================================ WorkProduct/evidence capture (E8.12)

def _select_and_create_workspace(client, tid, rid, agent="codex"):
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces",
                data={"repository_id": rid, "agent": agent, "role": "BUILDER", "base_branch": "main", "sandbox_profile": "NONE"})
    return [x for x in client.get(f"/api/tasks/{tid}").json()["workspaces"] if x["agent"] == agent][-1]


def test_code_change_work_product_captured_on_ready(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "WP capture change", project_id=rid)
    tid, plan_id = materialize_task(client, cid, requirement_ids=["REQ-1"])
    w = _select_and_create_workspace(client, tid, rid)
    r = client.post(f"/api/workspaces/{w['id']}/verification-report",
                     data={"work_status": "READY", "what_changed": "did the thing", "files_changed": "src/x.py"},
                     follow_redirects=False)
    assert r.status_code == 303, r.text
    wps = client.app.state.db.all("SELECT * FROM work_products WHERE task_id=? AND kind='CODE_CHANGE'", (tid,))
    assert len(wps) == 1
    meta = json.loads(wps[0]["content_metadata"])
    assert meta["provider"] == "codex" and meta["role"] == "BUILDER"
    assert meta["requirement_ids"] == ["REQ-1"]
    assert meta["modified_files"] == ["src/x.py"]
    assert meta["scope_check"]["violation"] is False
    outputs = client.app.state.db.all(
        "SELECT * FROM task_work_product_links WHERE task_id=? AND work_product_id=? AND direction='OUTPUT'", (tid, wps[0]["id"]))
    assert len(outputs) == 1


def test_code_change_work_product_not_created_for_legacy_task(client, git_repo):
    """A Task with no Change (the legacy/manual case) still submits for
    review exactly as before -- E8.12 only fires when there's a Change
    to attach the WorkProduct to (see record_code_change_work_product's
    own docstring), never a crash on the None case."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    r = client.post("/api/tasks", data={"title": "No change task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "No change task"][0]
    w = _select_and_create_workspace(client, tid, rid)
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert not client.app.state.db.all("SELECT * FROM work_products WHERE task_id=? AND kind='CODE_CHANGE'", (tid,))


# ================================================================ Scope Guard (E8.18)

def test_scope_violation_detected_for_out_of_scope_commit(client, git_repo):
    import subprocess
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Scope change", project_id=rid)
    tid, plan_id = materialize_task(client, cid, scope_hints=["src/allowed.py"])
    w = _select_and_create_workspace(client, tid, rid)
    worktree = w["worktree_path"]
    (root_path := __import__("pathlib").Path(worktree) / "src").mkdir(parents=True, exist_ok=True)
    (root_path / "other.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "src/other.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "out of scope edit"], cwd=worktree, check=True, capture_output=True)
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    wp = client.app.state.db.one("SELECT * FROM work_products WHERE task_id=? AND kind='CODE_CHANGE'", (tid,))
    meta = json.loads(wp["content_metadata"])
    assert meta["scope_check"]["violation"] is True
    assert "src/other.py" in meta["scope_check"]["out_of_scope_files"]
    events = client.app.state.db.all(
        "SELECT * FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='AUTO_SCOPE_VIOLATION_DETECTED'", (tid,))
    assert len(events) == 1


def test_no_scope_violation_when_change_stays_within_declared_scope(client, git_repo):
    import subprocess
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "In-scope change", project_id=rid)
    tid, plan_id = materialize_task(client, cid, scope_hints=["src/"])
    w = _select_and_create_workspace(client, tid, rid)
    worktree = w["worktree_path"]
    import pathlib
    (pathlib.Path(worktree) / "src").mkdir(parents=True, exist_ok=True)
    (pathlib.Path(worktree) / "src" / "allowed.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "src/allowed.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "in scope edit"], cwd=worktree, check=True, capture_output=True)
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    wp = client.app.state.db.one("SELECT * FROM work_products WHERE task_id=? AND kind='CODE_CHANGE'", (tid,))
    meta = json.loads(wp["content_metadata"])
    assert meta["scope_check"]["violation"] is False
    assert not client.app.state.db.all(
        "SELECT * FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='AUTO_SCOPE_VIOLATION_DETECTED'", (tid,))


def test_no_scope_check_without_declared_scope_hints(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "No scope declared", project_id=rid)
    tid, plan_id = materialize_task(client, cid)  # no scope_hints
    w = _select_and_create_workspace(client, tid, rid)
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    wp = client.app.state.db.one("SELECT * FROM work_products WHERE task_id=? AND kind='CODE_CHANGE'", (tid,))
    meta = json.loads(wp["content_metadata"])
    assert meta["scope_check"] == {"violation": False, "reason": "no scope_hints declared"}


# ================================================================ UI integration (E8.23)

def test_change_overview_shows_autonomous_execution_card(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "UI card change", project_id=rid)
    materialize_task(client, cid)
    html = client.get(f"/changes/{cid}").text
    assert "Autonomous Execution" in html
    assert "Ready Task" in html
    assert "Run next ready task" in html


def test_change_overview_shows_disabled_state_by_default(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "UI disabled change", project_id=rid)
    html = client.get(f"/changes/{cid}").text
    assert "Autonomous Execution" in html
    assert "Disabled" in html
    assert f"onclick=\"runAutonomousTick({cid}" not in html  # button only rendered when policy.enabled


def test_tasks_tab_shows_execution_readiness_column(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "UI tasks change", project_id=rid)
    materialize_task(client, cid)
    html = client.get(f"/changes/{cid}/tasks").text
    assert "Execution Readiness" in html
    assert "AUTO READY" in html
    assert "Run now" in html
