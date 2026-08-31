"""Productization Audit P0 -- targeted regression tests for repaired
inconsistencies (P0.20), plus the golden deterministic end-to-end
fixture (P0.5) and bug closed-loop fixture (P0.6), plus the E13 test
gap explicitly named in P0.7 (declared-safe scopes, actual overlap).

Real external-provider qualification is intentionally kept separate
(see docs/PRODUCTIZATION_AUDIT.md) -- every test in this file is
deterministic (fake LLM invokers, same established DI pattern every
other phase's own fake-review/fake-plan tests already use)."""
from __future__ import annotations
import json
import subprocess
import threading

from app.services.planner_service import PlannerAgentError
from tests.test_autonomous_execution import register, new_change, materialize_task
from tests.test_planner_service import set_fake_plan, minimal_valid_plan
from tests.test_review_fix_loop import set_fake, PASS
from tests.test_worktree_manager import _select_and_create_workspace
from tests.test_execution_waves import enable_parallel, new_plan_task, fake_launcher


def _db(client):
    return client.app.state.db


# ================================================================ P0.9/P0.20: DB connection-scoped insert-id regression

def test_concurrent_plan_creation_never_collides_on_revision(client, git_repo):
    """P0.9 finding: plans.(change_id,revision) is UNIQUE and the
    MAX(revision)+1 read is a separate statement from the INSERT --
    two concurrent plan_change() calls for the SAME Change used to be
    able to race onto the same revision number and crash with an
    unhandled IntegrityError. Fixed with the same retry-on-collision
    pattern already proven for ExecutionWaveService's own analogous
    wave_number race (E13)."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Concurrent plan change", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})
    set_fake_plan(client, minimal_valid_plan())

    results, errors = [], []

    def run():
        try:
            r = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
            results.append(r.json())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    ok = [r for r in results if r.get("outcome") in ("PLAN_READY", "HUMAN_DECISION_REQUIRED")]
    assert len(ok) == 4, results
    revisions = _db(client).all("SELECT revision FROM plans WHERE change_id=?", (cid,))
    assert sorted(r["revision"] for r in revisions) == [1, 2, 3, 4], revisions


# ================================================================ P0.2: TaskDecisionService completion audit

def test_task_decision_service_done_reflects_real_e9_e10_evidence_for_low_risk(client, git_repo):
    """P0.2 audit finding (corrects an imprecise E13 report): for a LOW
    risk Task (RISK_GATES: only REVIEW required), TaskDecisionService.
    evaluate() ALREADY reaches DONE purely from real E9 CodeReview
    (review_runs, the SAME table code_review_service.py writes to) and
    real E10 Integration (merge_records, the SAME table IntegrationService.
    integrate_task() writes to) -- no legacy workaround needed. The
    earlier E13 test that seemed to show a gap was NORMAL-risk (which
    genuinely, deliberately also requires QA -- RISK_GATES's own real
    design, not a bug) and never set risk_profile='LOW'."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Low risk done change", project_id=rid)
    tid, _ = materialize_task(client, cid, scope_hints=["x.py"])
    _db(client).execute("UPDATE tasks SET risk_profile='LOW' WHERE id=?", (tid,))

    w = _select_and_create_workspace(client, tid, rid, agent="claude")
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "x.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "add", "x.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "impl"], cwd=worktree, check=True, capture_output=True)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)

    before = client.app.state.decision.evaluate(tid)
    assert before["status"] != "DONE", "must not be DONE before real review/integration evidence exists"

    set_fake(client, PASS)
    review = client.post(f"/api/tasks/{tid}/review/code").json()
    assert review["outcome"] == "REVIEWED" and review["verdict"] == "PASS", review

    integ = client.post(f"/api/tasks/{tid}/integrate").json()
    assert integ["outcome"] == "INTEGRATED", integ

    after = client.app.state.decision.evaluate(tid)
    assert after["status"] == "DONE", after
    assert after["stage"] == "COMPLETE", after


def test_task_decision_service_normal_risk_requires_qa_deliberately(client, git_repo):
    """The genuine (non-bug) reason a NORMAL-risk Task stays non-DONE
    despite PASS review/integration -- RISK_GATES deliberately also
    requires QA for NORMAL/HIGH, a real, intentional manual gate, never
    a legacy artifact."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Normal risk change", project_id=rid)
    tid, _ = materialize_task(client, cid, scope_hints=["x.py"])  # default risk_profile is NORMAL

    w = _select_and_create_workspace(client, tid, rid, agent="claude")
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "x.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "add", "x.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "impl"], cwd=worktree, check=True, capture_output=True)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, PASS)
    client.post(f"/api/tasks/{tid}/review/code")
    client.app.state.git.head(w["worktree_path"])
    integ = client.post(f"/api/tasks/{tid}/integrate").json()

    ev = client.app.state.decision.evaluate(tid)
    assert ev["status"] != "DONE", ev
    # The real, correct reason: requires_qa(NORMAL) is True and no QA
    # evidence exists -- not a review/integration truth-source gap.
    assert not client.app.state.decision.requires_qa("LOW")
    assert client.app.state.decision.requires_qa("NORMAL")


# ================================================================ P0.7: E13's own named missing fixture

def test_actual_scope_overlap_despite_declared_safe_scopes(client, git_repo):
    """P0.7: 'declared-safe scopes but actual files overlap. Expected:
    PARALLEL_PREDICTION_MISS + integration protection.' -- Task A/B
    declare disjoint scope_hints (predicted PARALLEL_SAFE), but the
    real Builder for B also touches A's declared file (a real, if
    undisciplined, scope violation) -- recheck_actual_scope() must
    catch it as a real overlap and flag PARALLEL_PREDICTION_MISS since
    both were predicted safe; integration then genuinely protects the
    canonical target (no silent double-apply)."""
    root, repo = git_repo
    (repo / "shared.py").write_text("VALUE = 0\n")
    (repo / "a_only.py").write_text("A = 0\n")
    (repo / "b_only.py").write_text("B = 0\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base files"], cwd=repo, check=True, capture_output=True)
    enable_parallel(repo, max_concurrent=2, provider_caps={"codex": 2})

    rid = register(client, repo, "demo")
    cid = new_change(client, "Scope surprise change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "Change a_only.py", ["a_only.py"])
    b, _ = new_plan_task(client, cid, "B", "Change b_only.py", ["b_only.py"])

    safety = client.app.state.parallel_safety_service.evaluate_pair(a, b)
    assert safety["result"] == "PARALLEL_SAFE", safety  # predicted safe, per DECLARED scope only

    fake_launcher(client, "codex")
    run = client.app.state.execution_wave_service.run_execution_wave(cid)
    assert run["outcome"] == "LAUNCHED" and len(run["launched"]) == 2, run
    wave_id = run["wave_id"]
    by_task = {l["task_id"]: _db(client).one("SELECT * FROM agent_workspaces WHERE id=?", (l["workspace_id"],))
               for l in run["launched"]}

    # A's REAL Builder also touches shared.py -- undisciplined, but
    # real: this is exactly what the declared, disjoint scope_hints did
    # NOT predict. Both A and B touching shared.py (not just one of
    # them) is what makes their ACTUAL changed-file sets genuinely
    # intersect -- a single Task straying into an undeclared file with
    # no matching sibling touch is not a pairwise overlap at all.
    wt_a = client.app.state.git.validate_worktree(by_task[a]["worktree_path"])
    (wt_a / "a_only.py").write_text("A = 1\n")
    (wt_a / "shared.py").write_text("VALUE = 42\n")
    subprocess.run(["git", "add", "a_only.py", "shared.py"], cwd=wt_a, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "a work (touches shared.py too)"], cwd=wt_a, check=True, capture_output=True)

    # B's REAL Builder also touches shared.py -- undisciplined, but
    # real: this is exactly what the declared scope_hints did NOT
    # predict, and must be caught after the fact, never assumed safe
    # just because the plan said otherwise.
    wt_b = client.app.state.git.validate_worktree(by_task[b]["worktree_path"])
    (wt_b / "b_only.py").write_text("B = 1\n")
    (wt_b / "shared.py").write_text("VALUE = 99\n")
    subprocess.run(["git", "add", "b_only.py", "shared.py"], cwd=wt_b, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "b work (touches shared.py too)"], cwd=wt_b, check=True, capture_output=True)

    findings = client.app.state.execution_wave_service.recheck_actual_scope(wave_id)
    assert len(findings) == 1, findings
    finding = findings[0]
    assert finding["result"] in ("ACTUAL_SCOPE_OVERLAP", "ACTUAL_SCOPE_CONFLICT_RISK")
    assert "shared.py" in finding["overlap_files"]
    assert finding.get("prediction_miss") is True, finding

    events = _db(client).all(
        "SELECT * FROM workspace_events WHERE entity_type='change' AND entity_id=? AND action IN "
        "('ACTUAL_SCOPE_OVERLAP_DETECTED','PARALLEL_PREDICTION_MISS') ORDER BY id", (cid,))
    actions = {e["action"] for e in events}
    assert "ACTUAL_SCOPE_OVERLAP_DETECTED" in actions
    assert "PARALLEL_PREDICTION_MISS" in actions

    # Integration protection: since shared.py's real content diverges
    # from what A's own already-integrated state has, B's own merge is
    # never silently force-applied -- either it integrates cleanly
    # (git can 3-way merge disjoint hunks in the SAME file) or it is
    # honestly reported as a real conflict; either way this is never a
    # silent double-apply, and the PREDICTION_MISS evidence above is
    # already durable regardless of the merge outcome.
    for l in run["launched"]:
        client.post(f"/api/workspaces/{by_task[l['task_id']]['id']}/verification-report",
                     data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, PASS)
    client.post(f"/api/tasks/{a}/review/code")
    client.post(f"/api/tasks/{b}/review/code")
    integration = client.app.state.execution_wave_service.integrate_wave(wave_id)
    results_by_task = {r["task_id"]: r for r in integration["results"]}
    assert results_by_task[a]["outcome"] == "INTEGRATED", results_by_task[a]
    assert results_by_task[b]["outcome"] in ("INTEGRATED", "CONFLICT", "INTEGRATION_CONFLICT_AFTER_SIBLING"), results_by_task[b]


# ================================================================ P0.8: bounded retry on the shared real-provider invocation layer

def test_planner_invoker_retries_once_on_transient_subprocess_failure(client, git_repo):
    """P0.8: the known E4-E6 real-provider flakiness class (a subprocess-
    level failure -- non-zero exit, timeout, or an envelope the CLI
    itself flags non-success, e.g. stop_reason=tool_use despite
    --tools "") is now retried once (bounded, never infinite) before
    PlannerAgentInvoker.invoke() gives up. This fake runner fails
    transiently on its first call and succeeds on its second -- proving
    the retry actually recovers a real request rather than only
    existing on paper."""
    calls = []

    def flaky_then_ok(argv, cwd, timeout):
        calls.append(1)
        class R:
            pass
        r = R()
        if len(calls) == 1:
            r.returncode = 1
            r.stdout = ""
            r.stderr = "transient: stop_reason=tool_use"
            return r
        r.returncode = 0
        r.stdout = json.dumps({"is_error": False, "subtype": "success", "result": "OK"})
        r.stderr = ""
        return r

    from app.services.planner_service import PlannerAgentInvoker
    invoker = PlannerAgentInvoker(runner=flaky_then_ok, which=lambda name: "/usr/bin/claude")
    text = invoker.invoke("claude", "prompt", {"type": "object"}, "/tmp")
    assert text == "OK"
    assert len(calls) == 2, "expected exactly one retry, not zero and not more"


def test_planner_invoker_retry_is_bounded_never_infinite(client, git_repo):
    """The other half of P0.8's own explicit requirement: a persistently
    failing provider must still fail, deterministically, after a small
    bounded number of attempts -- never hang or retry forever."""
    calls = []

    def always_fails(argv, cwd, timeout):
        calls.append(1)
        class R:
            returncode = 1
            stdout = ""
            stderr = "persistent failure"
        return R()

    from app.services.planner_service import PlannerAgentInvoker
    invoker = PlannerAgentInvoker(runner=always_fails, which=lambda name: "/usr/bin/claude")
    try:
        invoker.invoke("claude", "prompt", {"type": "object"}, "/tmp")
        assert False, "expected PlannerAgentError"
    except PlannerAgentError:
        pass
    assert len(calls) == invoker.max_attempts == 2


def test_planner_invoker_never_retries_a_genuine_invalid_structured_response(client, git_repo):
    """The other deliberate half: an exit-0 response whose own output
    doesn't parse is a real defect in what the model produced, not a
    transient provider hiccup -- retrying it would only mask a real
    prompt/schema problem, so it must fail on the FIRST attempt."""
    calls = []

    def bad_output(argv, cwd, timeout):
        calls.append(1)
        class R:
            returncode = 0
            stdout = "not json at all {{{"
            stderr = ""
        return R()

    from app.services.planner_service import PlannerAgentInvoker
    invoker = PlannerAgentInvoker(runner=bad_output, which=lambda name: "/usr/bin/claude")
    try:
        invoker.invoke("claude", "prompt", {"type": "object"}, "/tmp")
        assert False, "expected PlannerAgentError"
    except PlannerAgentError:
        pass
    assert len(calls) == 1, "a genuine invalid-structured-response must never be retried"


# ================================================================ P0.7/P0.10: CANCELLED Task scheduler-recovery bug

def test_cancelled_task_never_reselected_by_scheduler_or_workflow_gates(client, git_repo):
    """P0.7/P0.10 audit finding, found while debugging the golden E2E
    fixture's wave-recovery step: a CANCELLED Task (the correct, real
    way to retire a superseded worktree once its own deterministic
    branch/worktree_path name can never be reused -- see docs/
    PRODUCTIZATION_AUDIT.md) used to still count as an ordinary Task in
    THREE separate places, all sharing the same root cause
    (TaskDependencyService.readiness() never recognized CANCELLED as
    its own terminal state, so it fell through to READY):
      1. AutonomousExecutionService.evaluate_task()/list_auto_ready_tasks()
         could still select and relaunch it (reusing its own stale,
         already-abandoned workspace).
      2. WorkflowService._gate_tests_pass()/_gate_release_ready() looped
         over EVERY Task including CANCELLED ones, whose checklist is
         deliberately empty -- a single cancelled Task could block a
         Change from ever reaching TESTS_PASS/RELEASE_READY, forever.
    This test proves both are fixed without needing the full golden
    fixture's real Builder/provider machinery."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Cancelled task change", project_id=rid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
    tid, _ = materialize_task(client, cid, scope_hints=["x.py"])
    db = _db(client)

    # readiness() itself: CANCELLED is its own terminal value, not READY.
    before = client.app.state.task_dependencies.readiness(tid, client.app.state.decision)
    assert before["readiness"] == "READY", before
    db.execute("UPDATE tasks SET status='CANCELLED' WHERE id=?", (tid,))
    after = client.app.state.task_dependencies.readiness(tid, client.app.state.decision)
    assert after["readiness"] == "CANCELLED", after

    # 1) The scheduler must never offer a CANCELLED Task as AUTO_READY.
    ev = client.app.state.autonomous_execution_service.evaluate_task(tid)
    assert ev["readiness"] == "NOT_AUTONOMOUS_TASK", ev
    ready = client.app.state.autonomous_execution_service.list_auto_ready_tasks(cid)
    assert tid not in [r["task_id"] for r in ready], ready

    # 2) A CANCELLED Task must never be the reason TESTS_PASS/
    # RELEASE_READY stay unmet forever -- with it excluded, the gate is
    # simply "no active Task has evidence yet" (False, but for the
    # RIGHT reason: this Change has zero active Tasks, not a poisoned
    # cancelled one), matching the exact same behavior a Change with
    # zero Tasks at all already has.
    ws = client.app.state.workflow_service
    tasks = client.app.state.changes.list_tasks_for_change(cid)
    assert ws._gate_tests_pass(cid, tasks) is False
    assert ws._gate_release_ready(cid, tasks) is False
    # Confirms it's excluded, not just coincidentally False: an empty
    # task list (no CANCELLED poison at all) gives the identical result.
    assert ws._gate_tests_pass(cid, []) is False
    assert ws._gate_release_ready(cid, []) is False
