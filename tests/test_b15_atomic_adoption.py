import multiprocessing as mp
import sqlite3
from pathlib import Path
import pytest
from scripts import harness_monitor_bridge as bridge
from app.services.agent_harness_mvp import HarnessStore, HarnessError

TID="dt_0123456789abcdea"
REG={name:{"automation_id":name,"title":name,"main_goal":name+" goal",
           "configured_enabled":True,"mode":"observe_and_register","role":"parent"}
     for name in ("b15-parent-alpha","b15-parent-beta")}
def backend():
    return {"status":"OK","result":{"task":{"task_id":TID,"target":"fixture",
             "agent":"codex","mode":"agent","state":"EXECUTION_ACCEPTED","dispatches":1}}}
def setup(db):
    HarnessStore(db);bridge.bootstrap(REG,db)
def adopt(db, owner, **kw):
    params=dict(child_goal=owner+" child",dod="verified evidence",agent="codex",
                node="fixture-node",session_name="fixture",task_id=TID,
                ttl_seconds=120,backend=backend())
    params.update(kw)
    return bridge.register(REG,db,owner,**params)
def snapshot(db):
    with sqlite3.connect(db) as c:
        return tuple(c.execute("SELECT count(*) FROM "+n).fetchone()[0] for n in
             ("harness_tasks","harness_monitor_receipts","harness_events"))
def claimant(path,owner,start,old_window,out):
    original=HarnessStore.create
    def synchronized(self,*a,**k):
        old_window.wait(10)
        return original(self,*a,**k)
    # Only legacy create is gated: deterministically expose its read/write race.
    HarnessStore.create=synchronized
    start.wait(10)
    try:out.put(("OK",adopt(Path(path),owner)["task_id"]))
    except HarnessError as exc:out.put(("DENIED",str(exc)))
    except Exception as exc:out.put(("ERROR",type(exc).__name__))
def test_atomic_rollback_on_receipt_insert_failure(tmp_path):
    db=tmp_path/"db";setup(db)
    with sqlite3.connect(db) as c:
        c.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON harness_monitor_receipts BEGIN SELECT RAISE(ABORT,'fixture reject'); END")
    with pytest.raises((HarnessError,sqlite3.IntegrityError)):adopt(db,"b15-parent-alpha")
    assert snapshot(db)==(0,0,0)
def test_two_process_claims_one_owner_no_orphan(tmp_path):
    db=tmp_path/"db";setup(db)
    ctx=mp.get_context("spawn");start=ctx.Barrier(2);old=ctx.Barrier(2);out=ctx.Queue()
    ps=[ctx.Process(target=claimant,args=(str(db),owner,start,old,out)) for owner in REG]
    try:
        for p in ps:p.start()
        results=[out.get(timeout=20),out.get(timeout=20)]
        for p in ps:p.join(5);assert p.exitcode==0
        assert sorted(v[0] for v in results)==["DENIED","OK"],results
        assert snapshot(db)==(1,1,2)
        with sqlite3.connect(db) as c:
            owner=c.execute("SELECT automation_id FROM harness_monitor_receipts").fetchone()[0]
        assert adopt(db,owner)["idempotent"] is True
        assert snapshot(db)==(1,1,2)
    finally:
        for p in ps:
            if p.is_alive():p.terminate();p.join(5)
def test_changed_identity_replay_refused(tmp_path):
    db=tmp_path/"db";setup(db);owner="b15-parent-alpha";adopt(db,owner)
    with pytest.raises(HarnessError):adopt(db,owner,node="different-node")
    assert snapshot(db)==(1,1,2)
