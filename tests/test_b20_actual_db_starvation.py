import pytest
from tests.test_autonomous_execution import enable_autonomous, register, new_change, materialize_task, create_workflow, fake_launcher
from tests.test_orchestration_v2_dispatch import _make_exited_v2_recovery_task

@pytest.mark.parametrize("mode", ["ownerless","unknown","running","backoff","retry_exhausted","ci_failure"])
def test_independent_dag_progress(client, git_repo, monkeypatch, mode):
    _,repo=git_repo
    enable_autonomous(repo)
    db=client.app.state.db
    svc=client.app.state.autonomous_execution_service
    if mode=="ownerless":
        rid=register(client,repo)
        blocked=new_change(client,"ownerless",project_id=rid)
        db.execute("UPDATE changes SET owner_name='' WHERE id=?",(blocked,))
        blocked_task,_=materialize_task(client,blocked)
    else:
        db,svc,blocked,blocked_task,sid=_make_exited_v2_recovery_task(client,repo)
        rid=db.one("SELECT project_id FROM changes WHERE id=?",(blocked,))["project_id"]
        if mode in ("unknown","running"):
            db.execute("UPDATE agent_sessions SET status='RUNNING' WHERE id=?",(sid,))
            svc.session_liveness=lambda _sid: mode=="running"
        else:
            for i in range(2 if mode=="retry_exhausted" else 1):
                db.event("task",blocked_task,"GOAL_RESUME_ATTEMPT",f"attempt={i+1}")
            svc.provider_available=lambda _agent:True
        if mode=="ci_failure":
            db.execute(
                "INSERT INTO merge_records(task_id,repository_id,required,merge_status,pr_number,pr_state,ci_status,mergeability,merge_state_status) "
                "VALUES(?,?,1,'PR_OPEN',123,'OPEN','FAIL','MERGEABLE','CLEAN')",
                (blocked_task,rid),
            )
            merge_record=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(blocked_task,rid))
            decision=svc.decision.evaluate(blocked_task)
            gate=svc.decision.merge_gate_status(decision,rid,merge_record)
            assert "CI_FAIL" in gate["blockers"]
            reconcile=svc.reconcile_change
            def failed_ci_is_change_local(change_id):
                if change_id==blocked:
                    return [{"change_id":blocked,"task_id":blocked_task,"outcome":"BLOCKED","code":"CI_FAIL",
                             "owner":"Change owner","next_action":"Repair the failed CI on this Change."}]
                return reconcile(change_id)
            monkeypatch.setattr(svc,"reconcile_change",failed_ci_is_change_local)
    svc.recovery_advisor=None
    svc._project_policy_resolver=lambda _c:{"autonomous_execution":{"enabled":True,"auto_start_ready_tasks":True,"goal_reconciliation_enabled":True,"max_concurrent_builders":1}}
    fake_launcher(client)
    create_workflow(client,blocked)
    dep=new_change(client,"dependent",project_id=rid)
    db.execute("UPDATE changes SET owner_name='fixture' WHERE id=?",(dep,))
    create_workflow(client,dep)
    _,plan=materialize_task(client,dep,task_type="REQUIREMENT_ANALYSIS",key="A")
    descendant,_=materialize_task(client,dep,key="B",depends_on_keys=["A"],plan_id=plan)
    ready=[]
    for name in ("independent one","independent two"):
        cid=new_change(client,name,project_id=rid)
        db.execute("UPDATE changes SET owner_name='fixture' WHERE id=?",(cid,))
        create_workflow(client,cid)
        tid,_=materialize_task(client,cid)
        ready.append(tid)
    assert svc.evaluate_task(descendant)["readiness"]=="WAITING_DEPENDENCY"
    for tid in ready:
        assert svc.evaluate_task(tid)["readiness"]=="AUTO_READY"
    session_count_before=db.one("SELECT COUNT(*) c FROM agent_sessions")["c"]
    launches=[]
    def launch(tid, repository_id, provider):
        assert svc._supervisor_lease_is_current()
        launches.append(tid)
        db.execute("UPDATE tasks SET status='CANCELLED' WHERE id=?",(tid,))
        return {"outcome":"LAUNCHED","task_id":tid}
    monkeypatch.setattr(svc,"_launch",launch)
    def forbidden(*a,**k):pytest.fail("blocked recovery must not start provider")
    monkeypatch.setattr(svc,"_start_builder_session",forbidden)
    first=svc.scheduled_reconcile_tick()
    expected={"ownerless":"BLOCKED","unknown":"UNKNOWN","running":"ALREADY_RUNNING","backoff":"BACKOFF","retry_exhausted":"BLOCKED","ci_failure":"BLOCKED"}[mode]
    assert any(x.get("change_id")==blocked and x.get("outcome")==expected for x in first["recovery"]),first
    if mode=="ci_failure":
        assert any(x.get("change_id")==blocked and x.get("code")=="CI_FAIL" for x in first["recovery"]),first
    assert first["outcome"]=="TICKED",first
    assert launches==ready[:1]
    assert svc.scheduled_reconcile_tick()["outcome"]=="TICKED"
    assert launches==ready
    assert svc.scheduled_reconcile_tick()["outcome"]=="TICKED"
    assert launches==ready
    assert svc.evaluate_task(descendant)["readiness"]=="WAITING_DEPENDENCY"
    assert db.one("SELECT COUNT(*) c FROM agent_sessions WHERE task_id IN (?,?)",tuple(ready))["c"]==0
    assert db.one("SELECT COUNT(*) c FROM agent_sessions")["c"]==session_count_before
