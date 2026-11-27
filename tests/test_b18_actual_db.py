"""Regression of fresh-to-durable-cache validation using synthetic provider receipts."""
import json
import time
import pytest
from app.services.recovery_decision_service import RecoveryDecisionService

REFS = ["task:9", "session:12"]
INCIDENT = {"task_id": 9, "failure": "UNKNOWN", "epoch": "b18-cache-contract", "owner": "owner-a"}


def _proposal():
    return dict(incident_id=RecoveryDecisionService.fingerprint(INCIDENT),
                evidence_refs=REFS[:], cause="synthetic ambiguity", confidence=.8,
                allowed_action="VERIFY", owner="owner-a", target="session:12",
                checkpoint="preserve", risk="NORMAL", expected_verification="readback",
                stop_condition="uncertain", rollback="none")


class _Invoker:
    def __init__(self, proposal=None, before_return=None):
        self.proposal = proposal
        self.calls = 0
        self.before_return = before_return

    def invoke(self, provider, prompt, schema, cwd, **kwargs):
        self.calls += 1
        assert self.proposal is not None, "unexpected provider call on cache path"
        assert provider == "claude" and kwargs["require_toolless"] is True
        if self.before_return:
            self.before_return()
        return dict(text=json.dumps(self.proposal), provider=provider,
                    resolved_model="claude-opus-5-5", effort_requested="high",
                    turns=1, cost_estimate_usd=0.0, toolless_verified=True)


def _advisor(db, path, invoker):
    return RecoveryDecisionService(db, invoker, path,
                                   provider_available=lambda p: p == "claude")


def _lease(db, owner="supervisor-a"):
    with db.connect() as conn:
        conn.execute("DELETE FROM orchestration_leases WHERE lease_key=?", ("projectflow-supervisor",))
        conn.execute("INSERT INTO orchestration_leases(lease_key,owner_id,expires_at) VALUES(?,?,?)",
                     ("projectflow-supervisor", owner, time.time() + 60))


def _rows(db):
    with db.connect() as conn:
        return [(r["id"], r["action"], r["details"]) for r in conn.execute(
            "SELECT id,action,details FROM workspace_events ORDER BY id")]


@pytest.mark.parametrize("field,value", [
    ("checkpoint", None), ("source", "FORGED"), ("incident_id", "different"),
    ("allowed_action", "LAUNCH"), ("confidence", True), ("confidence", float("nan")),
    ("risk", []), ("evidence_refs", [["task:9"]]), ("evidence_refs", ["task:other"]),
    ("owner", "other-owner"), ("target", "session:other"), ("rollback", 42),
])
def test_fresh_and_cached_invalid_proposal_fail_closed(client, tmp_path, field, value):
    db = client.app.state.db
    _lease(db)
    p = _proposal()
    if field == "checkpoint":
        del p[field]
    else:
        p[field] = value
    fresh = _Invoker(p)
    result = _advisor(db, tmp_path, fresh).advise(INCIDENT, REFS, lease_owner="supervisor-a")
    assert result["reason"] == "AI_PROPOSAL_INVALID"
    assert fresh.calls == 1 and not any(a == "RECOVERY_AI_PROPOSAL" for _, a, _ in _rows(db))
    db.event("task", 9, "RECOVERY_AI_PROPOSAL", json.dumps({
        "fingerprint": RecoveryDecisionService.fingerprint(INCIDENT), "proposal": p,
        "provider_receipt": {"toolless_verified": True},
    }))
    before = _rows(db)
    never = _Invoker()
    for _ in range(2):
        cached = _advisor(db, tmp_path, never).advise(INCIDENT, REFS, lease_owner="supervisor-a")
        assert cached == {"source": "DETERMINISTIC_FALLBACK", "allowed_action": "VERIFY",
                          "reason": "AI_CACHED_SCOPE_CHANGED"}
    assert never.calls == 0 and _rows(db) == before


def test_round_trip_revalidates_owner_and_refs_after_restart(client, tmp_path):
    db = client.app.state.db
    _lease(db)
    fresh = _Invoker(_proposal())
    first = _advisor(db, tmp_path, fresh).advise(INCIDENT, REFS, lease_owner="supervisor-a")
    assert first["source"] == "MODEL_ADVISORY" and fresh.calls == 1
    before = _rows(db)
    never = _Invoker()
    for _ in range(2):
        a = _advisor(db, tmp_path, never)
        assert a.advise(INCIDENT, REFS, lease_owner="supervisor-a")["source"] == "CACHED_MODEL_PROPOSAL"
        for incident, refs in [(dict(INCIDENT, owner="owner-b"), REFS), (INCIDENT, REFS[:1])]:
            assert a.advise(incident, refs, lease_owner="supervisor-a")["reason"] == "AI_CACHED_SCOPE_CHANGED"
    _lease(db, "successor")
    assert _advisor(db, tmp_path, never).advise(INCIDENT, REFS, lease_owner="supervisor-a")["reason"] == "SUPERVISOR_LEASE_LOST"
    assert never.calls == 0 and _rows(db) == before


def test_fresh_lease_takeover_cannot_publish(client, tmp_path):
    db = client.app.state.db
    _lease(db)
    fresh = _Invoker(_proposal(), lambda: _lease(db, "successor"))
    result = _advisor(db, tmp_path, fresh).advise(INCIDENT, REFS, lease_owner="supervisor-a")
    assert result["reason"] == "SUPERVISOR_LEASE_LOST" and fresh.calls == 1
    assert not any(a == "RECOVERY_AI_PROPOSAL" for _, a, _ in _rows(db))

from app.db import Database
from types import SimpleNamespace
import threading

@pytest.fixture
def client(tmp_path):
    db = Database(tmp_path / "actual-projectflow.sqlite")
    db.init()
    assert db.one("SELECT COUNT(*) AS n FROM schema_migrations")["n"] >= 43
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))

def receipt_for(provider, **updates):
    model = "gpt-6.2-astra" if provider == "codex" else "claude-opus-5-5"
    result = dict(text=json.dumps(_proposal()), provider=provider, resolved_model=model,
                  effort_requested="high", turns=1, cost_estimate_usd=0.0, toolless_verified=True)
    result.update(updates)
    return result

class ReceiptInvoker:
    def __init__(self, receipts):
        self.receipts = list(receipts)
        self.calls = []
    def advisory_toolless_capable(self, provider): return True
    def model_supported(self, provider, model, effort): return True
    def invoke(self, provider, prompt, schema, cwd, **kwargs):
        self.calls.append(provider)
        assert kwargs["require_toolless"] is True
        assert self.receipts, "unexpected invocation"
        return self.receipts.pop(0)

@pytest.mark.parametrize("case", ["none","list","string","wrong-provider","missing-provider",
                                  "wrong-effort","missing-effort","wrong-model","integer-tool-flag"])
def test_bad_receipt_real_database(client,tmp_path,case):
    db=client.app.state.db
    _lease(db)
    receipt=receipt_for("claude")
    if case=="none": receipt=None
    elif case=="list": receipt=[]
    elif case=="string": receipt="not a receipt"
    elif case=="wrong-provider": receipt["provider"]="codex"
    elif case=="missing-provider": del receipt["provider"]
    elif case=="wrong-effort": receipt["effort_requested"]="low"
    elif case=="missing-effort": del receipt["effort_requested"]
    elif case=="wrong-model": receipt["resolved_model"]="other"
    elif case=="integer-tool-flag": receipt["toolless_verified"]=1
    inv=ReceiptInvoker([receipt])
    result=_advisor(db,tmp_path,inv).advise(INCIDENT,REFS,lease_owner="supervisor-a")
    assert result=={"source":"DETERMINISTIC_FALLBACK","allowed_action":"VERIFY",
                    "reason":"EFFECTIVE_MODEL_RECEIPT_MISMATCH"}
    assert inv.calls==["claude"]
    before=_rows(db)
    assert [a for _,a,_ in before]==["RECOVERY_AI_ATTEMPT","RECOVERY_AI_CANDIDATE_UNVERIFIED","RECOVERY_AI_FALLBACK"]
    never=ReceiptInvoker([])
    for _ in range(2):
        restarted=Database(db.path)
        assert _advisor(restarted,tmp_path,never).advise(INCIDENT,REFS,lease_owner="supervisor-a")==result
    assert not never.calls and _rows(db)==before

@pytest.mark.parametrize("fallback",[False,True])
def test_dual_receipt_real_database(client,tmp_path,fallback):
    db=client.app.state.db
    _lease(db)
    preferred=receipt_for("codex")
    if fallback: preferred["provider"]="claude"
    inv=ReceiptInvoker([preferred,receipt_for("claude")] if fallback else [preferred])
    service=lambda db, inv: RecoveryDecisionService(db,inv,tmp_path,provider_available=lambda p:True)
    result=service(db,inv).advise(INCIDENT,REFS,lease_owner="supervisor-a")
    assert result["source"]=="MODEL_ADVISORY"
    assert inv.calls==(["codex","claude"] if fallback else ["codex"])
    before=_rows(db)
    assert sum(a=="RECOVERY_AI_PROPOSAL" for _,a,_ in before)==1
    assert sum(a=="RECOVERY_AI_ATTEMPT" for _,a,_ in before)==len(inv.calls)
    never=ReceiptInvoker([])
    for _ in range(2):
        a=service(Database(db.path),never)
        assert a.advise(INCIDENT,REFS,lease_owner="supervisor-a")["source"]=="CACHED_MODEL_PROPOSAL"
        assert a.advise(dict(INCIDENT,owner="owner-b"),REFS,lease_owner="supervisor-a")["reason"]=="AI_CACHED_SCOPE_CHANGED"
    assert not never.calls and _rows(db)==before

def test_two_database_instances_one_reservation(client,tmp_path):
    db=client.app.state.db
    _lease(db)
    entered=threading.Event()
    unblock=threading.Event()
    out=[]
    def hold():
        entered.set()
        assert unblock.wait(5)
    inv=_Invoker(_proposal(),hold)
    worker=threading.Thread(target=lambda:out.append(_advisor(Database(db.path),tmp_path,inv).advise(INCIDENT,REFS,lease_owner="supervisor-a")))
    worker.start()
    try:
        assert entered.wait(5)
        never=ReceiptInvoker([])
        second=_advisor(Database(db.path),tmp_path,never).advise(INCIDENT,REFS,lease_owner="supervisor-a")
        assert second["reason"]=="AI_COOLDOWN" and not never.calls
    finally:
        unblock.set()
        worker.join(6)
    assert not worker.is_alive() and len(out)==1 and out[0]["source"]=="MODEL_ADVISORY"
    assert inv.calls==1 and sum(a=="RECOVERY_AI_ATTEMPT" for _,a,_ in _rows(db))==1
