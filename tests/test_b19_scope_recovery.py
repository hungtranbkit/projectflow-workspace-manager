from pathlib import Path
import subprocess
import pytest
from tests.test_autonomous_execution import register, new_change, materialize_task, _select_and_create_workspace
from tests.test_orchestration_v2_dispatch import _make_exited_v2_recovery_task

def git(path,*args):
    return subprocess.check_output(["git","-C",str(path),*args],text=True,stderr=subprocess.PIPE).strip()

@pytest.mark.parametrize("kind",["untracked","bad_base","rename","staged_then_removed"])
def test_scope_cannot_report_unknown_or_outside_as_clean(client,git_repo,kind):
    _,repo=git_repo
    if kind=="rename":
        (repo/"allowed.py").write_text("original\n")
        git(repo,"add","allowed.py");git(repo,"commit","-m","fixture baseline")
    rid=register(client,repo)
    cid=new_change(client,"scope fixture",project_id=rid)
    tid,_=materialize_task(client,cid,scope_hints=["allowed.py"])
    w=_select_and_create_workspace(client,tid,rid)
    path=Path(w["worktree_path"])
    if kind=="untracked":(path/"outside.py").write_text("fixture only\n")
    elif kind=="bad_base":w=dict(w,base_commit="f"*40)
    elif kind=="rename":git(path,"mv","allowed.py","outside.py")
    else:
        (path/"outside.py").write_text("staged only")
        git(path,"add","outside.py")
        (path/"outside.py").unlink()
    svc=client.app.state.autonomous_execution_service
    task=client.app.state.db.one("SELECT * FROM tasks WHERE id=?",(tid,))
    result=svc.check_scope_violation(w,task)
    assert result["violation"] is True,result
    if kind=="bad_base":assert result["reason"]=="SCOPE_EVIDENCE_UNAVAILABLE",result
    else:assert "outside.py" in result["out_of_scope_files"],result

def test_recovery_refuses_outside_scope_before_provider_or_retry(client,git_repo):
    _,repo=git_repo
    db,svc,cid,tid,sid=_make_exited_v2_recovery_task(client,repo)
    ws=db.one("SELECT * FROM agent_workspaces WHERE task_id=?",(tid,))
    path=repo.parent/".worktrees"/"b19-scope-fixture"
    path.parent.mkdir(exist_ok=True)
    git(repo,"worktree","add","-b","agent/codex/b19-scope-fixture",str(path),"main")
    base=git(path,"rev-parse","HEAD")
    db.execute("UPDATE agent_workspaces SET worktree_path=?,base_commit=?,last_commit=?,branch=? WHERE id=?",(str(path),base,base,"agent/codex/b19-scope-fixture",ws["id"]))
    db.execute("UPDATE agent_sessions SET cwd=? WHERE id=?",(str(path),sid))
    plan=db.execute("INSERT INTO plans(change_id,revision,status,planner_provider,input_context_digest) VALUES(?,?,?,?,?)",(cid,1,"MATERIALIZED","claude","scope-fixture"))
    db.execute("INSERT INTO plan_items(plan_id,item_key,title,task_type,depends_on_keys,requirement_ids,scope_hints,materialized_task_id) VALUES(?,?,?,?,?,?,?,?)",(plan,"B19","fixture","IMPLEMENTATION","[]","[]",'["allowed.py"]',tid))
    (path/"outside.py").write_text("preserve me\n")
    svc.recovery_advisor=None
    # Denial must happen before querying a provider; no real model invocation.
    def forbidden(*args,**kwargs):pytest.fail("scope-blocked task reached provider/launch")
    svc.provider_available=forbidden
    svc._start_builder_session=forbidden
    for i in range(2):
        result=svc.reconcile_change(cid)[0]
        assert result["outcome"]=="BLOCKED" and result["code"]=="SCOPE_VIOLATION",result
    assert db.one("SELECT COUNT(*) c FROM agent_sessions WHERE task_id=?",(tid,))["c"]==1
    assert db.one("SELECT COUNT(*) c FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='GOAL_RESUME_ATTEMPT'",(tid,))["c"]==0
    assert db.one("SELECT COUNT(*) c FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='GOAL_RESUME_BLOCKED'",(tid,))["c"]==1
    assert (path/"outside.py").read_text()=="preserve me\n" and git(path,"rev-parse","HEAD")==base
