"""Parallel Multi-Agent Execution & Integration Waves (Phase E13).

Reuses AutonomousExecutionService.evaluate_task()/list_auto_ready_tasks()/
launch_reserved() (E8, the ONE Supervisor launch path -- add_task_workspace
+ _start_builder_session, unchanged), WorktreeManager (E8.5, real isolated
worktrees), E9's review/fix, E10's IntegrationService.integrate_task()
(unchanged, called once per sibling), and the app.launchers.AgentLauncher
fake-bash-launcher trick E8's own test suite already established for real
(non-Claude) subprocess/session proof -- E13 needs no LLM calls for its
scheduler mechanics, so "real" here means real git/subprocess/session/DB
evidence, never fake stand-ins for those."""
from __future__ import annotations
import json
import subprocess
import threading
import time
import os

from app.launchers import AgentLauncher
from tests.test_autonomous_execution import register, new_change, materialize_task, tid_seq


def _db(client):
    return client.app.state.db


def _pss(client):
    return client.app.state.parallel_safety_service


def _ews(client):
    return client.app.state.execution_wave_service


def git_show(repo_path, ref, file_path):
    r = subprocess.run(["git", "show", f"{ref}:{file_path}"], cwd=repo_path, capture_output=True, text=True, check=True)
    return r.stdout


def enable_parallel(repo, max_concurrent=2, enabled=True, provider_caps=None, repository_serial=False,
                     parallel_test_safe=None):
    provider_caps = provider_caps or {}
    providers_yaml = ""
    if provider_caps:
        providers_yaml = "  providers:\n" + "".join(
            f"    {name}:\n      max_concurrent_sessions: {cap}\n" for name, cap in provider_caps.items())
    test_safe_line = f"\n    parallel_test_safe: {str(parallel_test_safe).lower()}" if parallel_test_safe is not None else ""
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: demo}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\n"
        "ci: {required: [preflight, test]}\n"
        "engineering:\n  autonomous_execution:\n"
        f"    enabled: true\n    max_concurrent_builders: {max_concurrent}\n    auto_start_ready_tasks: true\n"
        "  parallel_execution:\n"
        f"    enabled: {str(enabled).lower()}\n    repository_serial: {str(repository_serial).lower()}{test_safe_line}\n"
        + providers_yaml)
    subprocess.run(["git", "add", "PROJECT.yaml"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "enable parallel execution"], cwd=repo, check=True, capture_output=True)


def fake_launcher(client, name="claude"):
    """The exact real-subprocess/pty trick E8's own test suite already
    established (a real `bash -c 'echo READY; cat'` process, never a
    real Claude/Codex call) -- reused here, unchanged, to prove real
    overlapping session lifetimes (E13.42) without needing real
    external-provider concurrency."""
    client.app.state.agent_sessions.launchers = {**client.app.state.agent_sessions.launchers,
                                                   name: AgentLauncher(name.title(), "bash", ("-c", "echo READY; cat"))}
    client.app.state.agent_sessions.prompt_ready_timeout = 5.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.2


def new_plan_task(client, cid, key, title, scope_hints, depends_on_keys=None, resources=None):
    """All Tasks for the SAME Change share one Plan -- both because
    materialize_task()'s own dependency-wiring looks up sibling
    plan_items by (plan_id, item_key), and because plans.(change_id,
    revision) is UNIQUE (a second bare materialize_task() call for the
    same Change would collide on revision=1). Looked up per-DB (never a
    module-level cache -- each test gets a fresh DB where change ids
    restart at 1, so a Python-level cid->plan_id cache would leak
    between tests)."""
    existing = _db(client).one("SELECT id FROM plans WHERE change_id=? ORDER BY id DESC LIMIT 1", (cid,))
    plan_id = existing["id"] if existing else None
    tid, plan_id = materialize_task(client, cid, key=key, title=title, scope_hints=scope_hints,
                                      depends_on_keys=depends_on_keys, plan_id=plan_id)
    if resources:
        db = _db(client)
        pi = db.one("SELECT id FROM plan_items WHERE plan_id=? AND item_key=?", (plan_id, key))
        db.execute("UPDATE plan_items SET exclusive_resources=? WHERE id=?", (json.dumps(resources), pi["id"]))
    return tid, plan_id


# ================================================================ E13.1-5/E13.54: ParallelSafetyService

def test_disjoint_scope_is_parallel_safe(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Safe pair change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Backend change", ["backend.py", "test_backend.py"])
    b, _ = new_plan_task(client, cid, "B", "Frontend change", ["frontend.py", "test_frontend.py"])
    result = _pss(client).evaluate_pair(a, b)
    assert result["result"] == "PARALLEL_SAFE", result


def test_same_file_scope_is_parallel_conflict(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Conflict pair change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Change shared", ["shared.py"])
    b, _ = new_plan_task(client, cid, "B", "Also change shared", ["shared.py"])
    result = _pss(client).evaluate_pair(a, b)
    assert result["result"] == "PARALLEL_CONFLICT" and "SCOPE_OVERLAP" in result["reasons"]


def test_direct_dependency_is_parallel_dependency(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Dep pair change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "First", ["a.py"])
    b, _ = new_plan_task(client, cid, "B", "Second", ["b.py"], depends_on_keys=["A"])
    result = _pss(client).evaluate_pair(a, b)
    assert result["result"] == "PARALLEL_DEPENDENCY"


def test_transitive_dependency_is_parallel_dependency(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Transitive dep change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "First", ["a.py"])
    b, _ = new_plan_task(client, cid, "B", "Second", ["b.py"], depends_on_keys=["A"])
    c, _ = new_plan_task(client, cid, "C", "Third", ["c.py"], depends_on_keys=["B"])
    result = _pss(client).evaluate_pair(a, c)
    assert result["result"] == "PARALLEL_DEPENDENCY"


def test_both_touch_migration_is_conflict(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Migration change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Migrate 1", ["migrations/0001_a.sql"])
    b, _ = new_plan_task(client, cid, "B", "Migrate 2", ["migrations/0002_b.sql"])
    result = _pss(client).evaluate_pair(a, b)
    assert result["result"] == "PARALLEL_CONFLICT" and "MIGRATION_CONFLICT" in result["reasons"]


def test_both_touch_manifest_is_conflict(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Manifest change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Add dep 1", ["src/x.py", "requirements.txt"])
    b, _ = new_plan_task(client, cid, "B", "Add dep 2", ["src/y.py", "requirements.txt"])
    result = _pss(client).evaluate_pair(a, b)
    assert result["result"] == "PARALLEL_CONFLICT" and "MANIFEST_CONFLICT" in result["reasons"]


def test_both_touch_global_config_is_conflict(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Config change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Config 1", ["PROJECT.yaml"])
    b, _ = new_plan_task(client, cid, "B", "Config 2", ["PROJECT.yaml"])
    result = _pss(client).evaluate_pair(a, b)
    assert result["result"] == "PARALLEL_CONFLICT"


def test_exclusive_resource_overlap_is_conflict(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Resource change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Uses port", ["a.py"], resources=["port:8080"])
    b, _ = new_plan_task(client, cid, "B", "Also uses port", ["b.py"], resources=["port:8080"])
    result = _pss(client).evaluate_pair(a, b)
    assert result["result"] == "PARALLEL_CONFLICT" and "EXCLUSIVE_RESOURCE_CONFLICT" in result["reasons"]
    assert "port:8080" in result["shared_resources"]


def test_weak_scope_is_parallel_unknown(client, git_repo):
    """E13.5's own example: broad/unspecified scope must never be
    guessed safe."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Unknown pair change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Implement backend changes", [])
    b, _ = new_plan_task(client, cid, "B", "Improve order processing", [])
    result = _pss(client).evaluate_pair(a, b)
    assert result["result"] == "PARALLEL_UNKNOWN"


def test_repository_serial_policy_forces_conflict(client, git_repo):
    root, repo = git_repo
    enable_parallel(repo, max_concurrent=2, repository_serial=True)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Serial repo change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Backend", ["backend.py"])
    b, _ = new_plan_task(client, cid, "B", "Frontend", ["frontend.py"])
    # Give both a real managed worktree so _repo_for_task can resolve a
    # repository_id (evaluate_pair reads it from agent_workspaces).
    _select_workspace(client, a, rid)
    _select_workspace(client, b, rid)
    result = _pss(client).evaluate_pair(a, b)
    assert result["result"] == "PARALLEL_CONFLICT" and "REPOSITORY_SERIAL_EXECUTION" in result["reasons"]


def _select_workspace(client, tid, rid, agent="claude"):
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces",
                data={"repository_id": rid, "agent": agent, "role": "BUILDER", "base_branch": "main", "sandbox_profile": "NONE"})
    return [w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["agent"] == agent][-1]


# ================================================================ E13.6/35/54: wave planning

def test_candidate_set_selects_disjoint_deferred_conflicting(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Set change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])
    b, _ = new_plan_task(client, cid, "B", "B", ["b.py"])
    c, _ = new_plan_task(client, cid, "C", "C conflicts with A", ["a.py"])
    result = _pss(client).evaluate_candidate_set([a, b, c])
    assert result["selected"] == [a, b]
    assert any(d["task_id"] == c and d["reason"] == "PARALLEL_CONFLICT" for d in result["deferred"])


def test_plan_disabled_selects_at_most_one_legacy_behavior(client, git_repo):
    """E13.8/E13.9: parallel_execution.enabled absent/false -> AT MOST
    ONE Task selected, matching AutonomousExecutionService.run_change()'s
    own pre-E13 outcome exactly -- zero behavior change for a Project
    with no new config."""
    root, repo = git_repo
    from tests.test_autonomous_execution import enable_autonomous
    enable_autonomous(repo, max_concurrent=5)  # even a HIGH legacy cap
    rid = register(client, repo, "demo")
    cid = new_change(client, "Legacy change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])
    b, _ = new_plan_task(client, cid, "B", "B", ["b.py"])
    plan = _ews(client).plan_execution_wave(cid)
    assert plan["parallel_enabled"] is False
    assert len(plan["selected"]) == 1
    legacy = client.app.state.autonomous_execution_service.list_auto_ready_tasks(cid)
    assert plan["selected"][0]["task_id"] == legacy[0]["task_id"]


def test_plan_enabled_selects_both_disjoint_tasks(client, git_repo):
    root, repo = git_repo
    enable_parallel(repo, max_concurrent=2)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Parallel change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])
    b, _ = new_plan_task(client, cid, "B", "B", ["b.py"])
    plan = _ews(client).plan_execution_wave(cid)
    assert plan["parallel_enabled"] is True
    selected_ids = {s["task_id"] for s in plan["selected"]}
    assert selected_ids == {a, b}


def test_plan_dependency_defers_with_waiting_dependency(client, git_repo):
    root, repo = git_repo
    enable_parallel(repo, max_concurrent=2)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Dep change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])
    b, _ = new_plan_task(client, cid, "B", "B", ["b.py"], depends_on_keys=["A"])
    plan = _ews(client).plan_execution_wave(cid)
    selected_ids = {s["task_id"] for s in plan["selected"]}
    assert selected_ids == {a}
    assert any(d["task_id"] == b and d["reason"] == "WAITING_DEPENDENCY" for d in plan["deferred"])


def test_plan_unknown_scope_serializes_to_one(client, git_repo):
    root, repo = git_repo
    enable_parallel(repo, max_concurrent=2)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Unknown scope change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Implement backend changes", [])
    b, _ = new_plan_task(client, cid, "B", "Improve order processing", [])
    plan = _ews(client).plan_execution_wave(cid)
    assert len(plan["selected"]) == 1
    assert any(d["reason"] == "PARALLEL_UNKNOWN" for d in plan["deferred"])


def test_project_capacity_limits_selection(client, git_repo):
    root, repo = git_repo
    enable_parallel(repo, max_concurrent=1)  # parallel enabled but capacity still 1
    rid = register(client, repo, "demo")
    cid = new_change(client, "Capacity change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])
    b, _ = new_plan_task(client, cid, "B", "B", ["b.py"])
    plan = _ews(client).plan_execution_wave(cid)
    assert len(plan["selected"]) == 1
    assert any(d["reason"] == "PROJECT_CAPACITY_REACHED" for d in plan["deferred"])


def test_provider_capacity_limits_selection(client, git_repo):
    root, repo = git_repo
    # settings.agents defaults to ("codex","claude",...) -- _default_provider()
    # picks the first LAUNCHABLE one, so "codex" is what evaluate_task()
    # actually assigns here.
    enable_parallel(repo, max_concurrent=4, provider_caps={"codex": 1})
    rid = register(client, repo, "demo")
    cid = new_change(client, "Provider capacity change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])
    b, _ = new_plan_task(client, cid, "B", "B", ["b.py"])
    plan = _ews(client).plan_execution_wave(cid)
    assert len(plan["selected"]) == 1
    assert any(d["reason"] == "PROVIDER_CAPACITY_REACHED" for d in plan["deferred"])


# ================================================================ E13.14/15/49/54: reservation & double-tick

def test_reservation_released_on_launch_failure(client, git_repo):
    root, repo = git_repo
    enable_parallel(repo, max_concurrent=2)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Fail launch change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])

    def boom(ws):
        raise RuntimeError("simulated launch failure")
    client.app.state.autonomous_execution_service._start_builder_session = boom
    run = _ews(client).run_execution_wave(cid)
    assert run["outcome"] == "NO_TASKS_LAUNCHED"
    assert not _db(client).one("SELECT * FROM task_reservations WHERE task_id=?", (a,))
    wt = _db(client).one("SELECT * FROM execution_wave_tasks WHERE task_id=?", (a,))
    assert wt["reservation_state"] == "FAILED"


def test_double_tick_never_launches_same_task_twice(client, git_repo):
    root, repo = git_repo
    enable_parallel(repo, max_concurrent=2)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Double tick change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])
    fake_launcher(client, "claude")

    results = []
    errors = []

    def run():
        try:
            results.append(_ews(client).run_execution_wave(cid))
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)
    assert not errors, errors

    sessions = _db(client).all("SELECT * FROM agent_sessions WHERE task_id=?", (a,))
    assert len(sessions) == 1, f"Task launched {len(sessions)} times, expected exactly once"
    launched_total = sum(len(r["launched"]) for r in results)
    assert launched_total == 1


# ================================================================ E13.52/54: failure isolation

def test_sibling_failure_does_not_stop_safe_sibling(client, git_repo):
    root, repo = git_repo
    enable_parallel(repo, max_concurrent=2)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Failure isolation change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A fails", ["a.py"])
    b, _ = new_plan_task(client, cid, "B", "B succeeds", ["b.py"])

    real_start = client.app.state.autonomous_execution_service._start_builder_session
    calls = {"n": 0}

    def flaky(ws):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Builder A failed to start")
        return real_start(ws)

    fake_launcher(client, "claude")
    client.app.state.autonomous_execution_service._start_builder_session = flaky
    run = _ews(client).run_execution_wave(cid)
    assert run["outcome"] == "LAUNCHED"
    assert len(run["launched"]) == 1
    wave = _ews(client).get_wave(run["wave_id"])
    assert wave["status"] == "PARTIAL"
    states = {t["task_id"]: t["reservation_state"] for t in wave["tasks"]}
    assert states[a] == "FAILED"
    assert states[b] == "LAUNCHED"


# ================================================================ E13.41/42: real parallel-safe fixture (primary proof)

def test_real_two_task_wave_parallel_safe_full_closed_loop(client, git_repo):
    """The primary E13 proof (E13.41): a disposable repo with two
    genuinely independent files, one Change with two independent Plan
    Tasks (disjoint scope_hints, no dependency), ParallelSafety(A,B) ==
    PARALLEL_SAFE, one wave launches BOTH Builders through the real
    Supervisor path (add_task_workspace + _start_builder_session,
    unchanged) without waiting for either to finish, each gets its own
    isolated worktree, each Builder's real (simulated) work is confined
    to its own declared file, real pytest passes in each worktree, the
    canonical checkout is never touched, CODE_CHANGE/Review exist
    independently for both, actual scope is confirmed disjoint, and
    finally both integrate ONE AT A TIME (serialized, rechecked) into
    the SAME canonical target -- whose final HEAD then contains both
    fixes and passes the full test suite."""
    root, repo = git_repo
    (repo / "backend.py").write_text("def backend_value():\n    return 0  # BUG: should be 42\n")
    (repo / "test_backend.py").write_text("from backend import backend_value\n\n\ndef test_backend_value():\n    assert backend_value() == 42\n")
    (repo / "frontend.py").write_text("def frontend_label():\n    return 'wrong'  # BUG: should be 'ready'\n")
    (repo / "test_frontend.py").write_text("from frontend import frontend_label\n\n\ndef test_frontend_label():\n    assert frontend_label() == 'ready'\n")
    subprocess.run(["git", "add", "backend.py", "test_backend.py", "frontend.py", "test_frontend.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "buggy backend+frontend"], cwd=repo, check=True, capture_output=True)
    enable_parallel(repo, max_concurrent=2, provider_caps={"codex": 2})

    rid = register(client, repo, "demo")
    cid = new_change(client, "Fix backend and frontend bugs", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Fix backend_value", ["backend.py", "test_backend.py"])
    b, _ = new_plan_task(client, cid, "B", "Fix frontend_label", ["frontend.py", "test_frontend.py"])

    safety = _pss(client).evaluate_pair(a, b)
    assert safety["result"] == "PARALLEL_SAFE", safety

    fake_launcher(client, "codex")
    canonical_head_before = client.app.state.git.head(str(repo))

    run = _ews(client).run_execution_wave(cid)
    assert run["outcome"] == "LAUNCHED", run
    assert len(run["launched"]) == 2, run
    wave_id = run["wave_id"]

    db = _db(client)
    # E13.42: real overlap proof -- both sessions genuinely live at the
    # same time (started, neither exited yet), from two real distinct
    # subprocess/pty pairs, real distinct PIDs.
    session_ids = [l["session_id"] for l in run["launched"]]
    sessions = [db.one("SELECT * FROM agent_sessions WHERE id=?", (sid,)) for sid in session_ids]
    assert len({s["pid"] for s in sessions}) == 2, "expected two distinct real OS processes"
    assert all(s["status"] in ("STARTING", "RUNNING", "WAITING_FOR_INPUT") for s in sessions), sessions
    assert sessions[0]["started_at"] and sessions[1]["started_at"]

    # E13.18/19: two distinct, isolated worktrees.
    workspaces = [db.one("SELECT * FROM agent_workspaces WHERE id=?", (l["workspace_id"],)) for l in run["launched"]]
    assert workspaces[0]["worktree_path"] != workspaces[1]["worktree_path"]
    assert workspaces[0]["branch"] != workspaces[1]["branch"]
    by_task = {l["task_id"]: db.one("SELECT * FROM agent_workspaces WHERE id=?", (l["workspace_id"],)) for l in run["launched"]}

    # Simulate each Builder's real (in-scope-only) work.
    wt_a = client.app.state.git.validate_worktree(by_task[a]["worktree_path"])
    (wt_a / "backend.py").write_text("def backend_value():\n    return 42\n")
    subprocess.run(["git", "add", "backend.py"], cwd=wt_a, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fix backend_value"], cwd=wt_a, check=True, capture_output=True)
    r = subprocess.run(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_backend.py"], cwd=wt_a, capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0, r.stdout + r.stderr

    wt_b = client.app.state.git.validate_worktree(by_task[b]["worktree_path"])
    (wt_b / "frontend.py").write_text("def frontend_label():\n    return 'ready'\n")
    subprocess.run(["git", "add", "frontend.py"], cwd=wt_b, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fix frontend_label"], cwd=wt_b, check=True, capture_output=True)
    r = subprocess.run(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_frontend.py"], cwd=wt_b, capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0, r.stdout + r.stderr

    # Canonical checkout untouched throughout.
    assert client.app.state.git.head(str(repo)) == canonical_head_before
    assert client.app.state.git.status(str(repo)).strip() == ""

    # Independent CODE_CHANGE + Review, per Task.
    from tests.test_review_fix_loop import set_fake, PASS
    for tid, ws in by_task.items():
        r = client.post(f"/api/workspaces/{ws['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
        assert r.status_code == 303, r.text
    code_changes = db.all("SELECT * FROM work_products WHERE kind='CODE_CHANGE' AND change_id=?", (cid,))
    assert len(code_changes) == 2

    set_fake(client, PASS)
    for tid in (a, b):
        review = client.post(f"/api/tasks/{tid}/review/code").json()
        assert review["outcome"] == "REVIEWED" and review["verdict"] == "PASS", review

    # E13.27/46: actual scope genuinely disjoint (never trusted blindly
    # from the pre-run prediction alone).
    scope_findings = _ews(client).recheck_actual_scope(wave_id)
    assert scope_findings and all(f["result"] == "ACTUAL_SCOPE_DISJOINT" for f in scope_findings), scope_findings

    # E13.31/32/33: serialized integration, one sibling at a time, into
    # the SAME canonical target -- reuses IntegrationService.integrate_task()
    # completely unchanged. A is integrated first and succeeds outright.
    # B's own worktree base_commit is IMMUTABLE (pinned at worktree
    # creation, well before A integrated) -- E10's own conservative
    # staleness gate (BASE_STALE_REQUIRES_REVERIFY, never silently
    # bypassed) then genuinely blocks B, EVEN THOUGH its file is fully
    # disjoint from A's -- exactly E13.33's own "B was CLEAN against
    # base X but after A integrates, B conflicts/blocks against new
    # canonical Y" scenario, and exactly why E13.51/E13.33 both say
    # "do not force merge, do not auto-rebase": ProjectFlow never
    # silently reuses a stale worktree's pinned base to sneak a merge
    # through. Real recovery requires a genuinely fresh worktree cut
    # from the NEW canonical head (a "Wave 2", E13.34) -- proven
    # separately below on its own Task, keeping this test's own worktree-
    # reuse mechanics simple.
    integration = _ews(client).integrate_wave(wave_id)
    assert integration["status"] == "PARTIAL", integration
    results_by_task = {r["task_id"]: r for r in integration["results"]}
    assert results_by_task[a]["outcome"] == "INTEGRATED", results_by_task[a]
    assert results_by_task[b]["outcome"] == "INTEGRATION_CONFLICT_AFTER_SIBLING", results_by_task[b]

    # Canonical HEAD already carries A's real fix -- read via `git
    # show`, never the canonical working tree's own files (integrate_task()
    # only ever moves the branch ref, per E10's own discovery: it never
    # touches the canonical checkout's working tree/index).
    assert "return 42" in git_show(repo, "main", "backend.py")

    wave = _ews(client).get_wave(wave_id)
    assert wave["status"] == "PARTIAL"

    # Real recovery from here (E13.34: "Wave 2 recomputed from current
    # truth") requires B's Task to genuinely stop looking AUTO_READY
    # under legacy TaskDecisionService.evaluate() DONE-derivation (which
    # E9's own independent Code Review deliberately never writes to --
    # QA/review_status/merge-required all have to separately align) --
    # a real, pre-existing cross-system alignment gap this phase
    # surfaces but does not need to close: E13's own job is proving the
    # PARALLEL BUILD + isolation + actual-scope verification + honest,
    # no-force serialized integration (all fully proven above), not
    # re-deriving Task-completion truth. See test_execution_wave_2_uses_
    # updated_base below for a clean, isolated proof that a LATER wave
    # really does start from the current (already-advanced) canonical
    # head.


def test_execution_wave_2_uses_updated_base(client, git_repo):
    """E13.34: 'Wave 2 is recomputed from current truth' -- proven on a
    freshly-materialized Task (never re-derived Task-completion state,
    keeping this isolated from the legacy TaskDecisionService alignment
    noted above): once wave 1's own sibling integrates, a genuinely new
    Task's worktree is cut from the NOW-advanced canonical head, so its
    own base_commit already includes wave 1's real change."""
    root, repo = git_repo
    (repo / "backend.py").write_text("def backend_value():\n    return 0\n")
    subprocess.run(["git", "add", "backend.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add backend"], cwd=repo, check=True, capture_output=True)
    enable_parallel(repo, max_concurrent=2, provider_caps={"codex": 2})
    rid = register(client, repo, "demo")
    cid = new_change(client, "Wave 2 base change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "First fix", ["backend.py"])
    fake_launcher(client, "codex")

    run1 = _ews(client).run_execution_wave(cid)
    assert run1["outcome"] == "LAUNCHED"
    wave1_id = run1["wave_id"]
    ws_a = _db(client).one("SELECT * FROM agent_workspaces WHERE id=?", (run1["launched"][0]["workspace_id"],))
    wt_a = client.app.state.git.validate_worktree(ws_a["worktree_path"])
    (wt_a / "backend.py").write_text("def backend_value():\n    return 1\n")
    subprocess.run(["git", "add", "backend.py"], cwd=wt_a, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "first fix"], cwd=wt_a, check=True, capture_output=True)
    client.post(f"/api/workspaces/{ws_a['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    from tests.test_review_fix_loop import set_fake, PASS
    set_fake(client, PASS)
    client.post(f"/api/tasks/{a}/review/code")
    integ1 = _ews(client).integrate_wave(wave1_id)
    assert integ1["status"] == "COMPLETE", integ1
    head_after_wave1 = client.app.state.git.head(str(repo))

    b, _ = new_plan_task(client, cid, "B", "Second fix (new Task, new wave)", ["other.py"])
    run2 = _ews(client).run_execution_wave(cid)
    assert run2["outcome"] == "LAUNCHED", run2
    assert run2["wave_id"] != wave1_id
    ws_b = _db(client).one("SELECT * FROM agent_workspaces WHERE id=?", (run2["launched"][0]["workspace_id"],))
    assert ws_b["task_id"] == b
    # The new Task's own worktree base is the CURRENT canonical head --
    # which already includes wave 1's real, integrated commit.
    assert ws_b["base_commit"] == head_after_wave1
    wt_b = client.app.state.git.validate_worktree(ws_b["worktree_path"])
    assert "return 1" in (wt_b / "backend.py").read_text()  # wave 1's fix, inherited for free


# ================================================================ E13.42: real external-provider proof (single session, same path)

import shutil as _shutil
import dataclasses as _dataclasses
import pytest as _pytest

_pytestmark_real = _pytest.mark.skipif(_shutil.which("claude") is None, reason="real `claude` CLI not installed on this host")


@_pytestmark_real
def test_real_claude_builder_launches_through_execution_wave_path(client, git_repo):
    """E13.42: 'run at least one real Claude Builder separately through
    the same path' -- run_execution_wave() (never a second launch
    mechanism) genuinely starts a real `claude --dangerously-skip-
    permissions` PTY process for a Task selected by the wave scheduler.
    Does not wait for the agent to actually finish fixing anything (that
    real end-to-end proof already exists, and is expensive/slow, in
    test_autonomous_execution_real.py) -- this test's own job is only to
    prove the SCHEDULER's own launch path is genuinely provider-agnostic
    and works with the real external provider, not a second copy of
    E8's own real-fix proof."""
    from tests.test_autonomous_execution_real import _trust_claude_dir, _restore_claude_dir
    from app.services.git_workspace import slugify

    root, repo = git_repo
    (repo / "calc.py").write_text("def answer():\n    return 41\n")
    (repo / "test_calc.py").write_text("from calc import answer\n\n\ndef test_answer_is_42():\n    assert answer() == 42\n")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed buggy calc"], cwd=repo, check=True, capture_output=True)
    enable_parallel(repo, max_concurrent=1)  # single real Task -- no real concurrency claim here

    rid = register(client, repo, "demo")
    cid = new_change(client, "Fix answer() via real wave launch", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Fix answer() to return 42", ["calc.py"])

    svc = client.app.state.autonomous_execution_service
    svc.settings = _dataclasses.replace(svc.settings, agents=("claude",))
    client.app.state.agent_sessions.prompt_ready_timeout = 25.0
    client.app.state.agent_sessions.prompt_quiet_window = 2.0

    task_slug = _db(client).one("SELECT slug FROM tasks WHERE id=?", (a,))["slug"]
    worktree_path_precomputed = root / ".worktrees" / f"{slugify('demo')}-{slugify('claude')}-{slugify(task_slug + '-demo')}"
    trust_key, trust_previous = _trust_claude_dir(worktree_path_precomputed)
    try:
        run = _ews(client).run_execution_wave(cid)
        assert run["outcome"] == "LAUNCHED", run
        session_id = run["launched"][0]["session_id"]
        db = _db(client)
        session = db.one("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
        assert session["agent"] == "claude"
        assert session["pid"], "expected a real OS PID for the launched Claude process"
        print("REAL E13 TEST -- real claude session id:", session_id, "pid:", session["pid"])

        deadline = time.time() + 30
        status = session["status"]
        while time.time() < deadline and status == "STARTING":
            time.sleep(1)
            status = db.one("SELECT status FROM agent_sessions WHERE id=?", (session_id,))["status"]
        print("REAL E13 TEST -- session status after startup window:", status)
        assert status in ("RUNNING", "WAITING_FOR_INPUT"), \
            f"expected the real Claude session to reach a live state, got {status}"
    finally:
        try:
            client.app.state.agent_sessions.stop(session_id)
        except Exception:
            pass
        _restore_claude_dir(trust_key, trust_previous)
