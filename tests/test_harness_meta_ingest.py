"""Issue #52 GAP_OBSERVABILITY: Harness record -> cycle ledger -> report.

Deterministic fixtures only (tmp Harness DB + summarize() snapshot). These are
NOT runtime acceptance of the production Meta-Monitor.
"""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from app.services.agent_harness_mvp import HarnessStore
from scripts import harness_meta_monitor as mm
from scripts.harness_monitor_bridge import bootstrap, load_registry
from scripts.harness_progress import summarize

NOW = datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc)
ZGH = "Automation_eec08a5752d08191b3de371065c27320"
SCREEN = "6af698c9a5308191899c6a7e013cee5f"


@pytest.fixture
def env(tmp_path):
    hdb = tmp_path / "harness.db"
    HarnessStore(hdb)
    bootstrap(load_registry(), hdb)
    return {"hdb": hdb, "ledger": tmp_path / "meta.db", "tmp": tmp_path}


def snap(env, at, healthy=True):
    s = summarize(env["hdb"], reference=at)
    s["health"] = {"status": "HEALTHY" if healthy else "FAILED_OR_STALE"}
    return s


def ingest(env, s, at, ids=(ZGH,)):
    with mm.ledger(env["ledger"], create=True) as db:
        return mm.ingest_snapshot(db, s, env["hdb"], at, list(ids))


def rows(env, monitor=ZGH):
    with mm.ledger(env["ledger"]) as db:
        return mm.cycles(db, monitor)


def goal(monitor=ZGH):
    return load_registry()[monitor]["main_goal"]


def finish_task(env, created, *, canary=False, key="t1"):
    store = HarnessStore(env["hdb"])
    t = store.create(key=key, main_goal=goal(),
                     child_goal=("CANARY ONLY: probe " if canary else "Fix dispatch ") + key,
                     dod="independent test", agent="codex", node="dell-linux",
                     created_at=created, ttl=timedelta(hours=2))
    with sqlite3.connect(env["hdb"]) as c:   # emulate the reconciler's verified finish
        c.execute("""UPDATE harness_tasks SET state='DONE_VERIFIED', verified_at=?,
                     cleanup_state='CLEANUP_PENDING' WHERE id=?""",
                  ((created + timedelta(minutes=20)).isoformat(), t.id))
    return t


# --------------------------------------------------------------- freshness
def test_first_fresh_cycle_is_recorded_with_unknown_deltas(env):
    out = ingest(env, snap(env, NOW), NOW + timedelta(minutes=1))
    assert out[ZGH]["status"] == "RECORDED"
    r = rows(env)[0]
    assert r["provenance"] == "HARNESS_SNAPSHOT"
    assert r["readback_status"] == "VERIFIED_READBACK" and r["readback_age_seconds"] == 60
    assert r["gate_delta"] is None and r["execution_delta"] is None   # no prior => UNKNOWN
    assert r["blocker_code"] is None and r["owner_intervention"] is None


def test_stale_or_unhealthy_snapshot_records_unknown_never_scores(env):
    s = snap(env, NOW)
    ingest(env, s, NOW + timedelta(minutes=20))                      # stale (>900s)
    ingest(env, snap(env, NOW + timedelta(minutes=25), healthy=False),
           NOW + timedelta(minutes=26))
    rs = rows(env)
    assert [r["readback_status"] for r in rs] == ["HARNESS_UNKNOWN"] * 2
    assert all(r["gate_delta"] is None and r["execution_delta"] is None for r in rs)
    m = mm.l2_metrics(rs, NOW + timedelta(minutes=30))
    assert m["stale_readback_rate"] == 1.0 and m["useful_action_rate"] is None


def test_paused_or_unknown_automation_is_skipped_not_recorded(env):
    out = ingest(env, snap(env, NOW), NOW, ids=("6af6notreal",))
    assert out["6af6notreal"]["status"] == "SKIPPED_NOT_ENABLED"
    with mm.ledger(env["ledger"]) as db:
        assert mm.cycles(db) == []


# ------------------------------------------------------ idempotency / dedupe
def test_duplicate_delivery_is_idempotent(env):
    s = snap(env, NOW)
    ingest(env, s, NOW + timedelta(minutes=1))
    again = ingest(env, s, NOW + timedelta(minutes=2))
    assert again[ZGH]["status"] == "ALREADY_RECORDED"
    stale1 = ingest(env, s, NOW + timedelta(minutes=31))
    stale2 = ingest(env, s, NOW + timedelta(minutes=32))   # same 5-min bucket
    assert stale1[ZGH]["status"] == "RECORDED"
    assert stale2[ZGH]["status"] == "ALREADY_RECORDED"
    assert len(rows(env)) == 2


def test_conflicting_redelivery_for_same_cycle_is_refused(env):
    s = snap(env, NOW)
    ingest(env, s, NOW + timedelta(minutes=1))
    forged = json.loads(json.dumps(s))
    zgh = next(m for m in forged["monitors"] if m["id"] == ZGH)
    for g in zgh["gates"]:
        g["status"] = "PASSED"
    out = ingest(env, forged, NOW + timedelta(minutes=2))
    assert out[ZGH]["status"] == "REFUSED"
    assert "CONFLICT" in out[ZGH]["reason"]
    assert len(rows(env)) == 1


def test_out_of_order_snapshot_refused(env):
    ingest(env, snap(env, NOW), NOW + timedelta(minutes=1))
    old = snap(env, NOW - timedelta(minutes=5))
    out = ingest(env, old, NOW - timedelta(minutes=4))
    assert out[ZGH]["status"] == "REFUSED" and "OUT_OF_ORDER" in out[ZGH]["reason"]


# ------------------------------------------------------- evidence of delta
def test_flat_gates_and_no_task_is_observed_zero_delta(env):
    ingest(env, snap(env, NOW), NOW)
    ingest(env, snap(env, NOW + timedelta(minutes=5)), NOW + timedelta(minutes=5))
    r = rows(env)[-1]
    assert (r["gate_delta"], r["execution_delta"]) == (0, 0)


def test_new_passed_gate_is_gate_delta_from_gate_acceptance(env):
    s1 = snap(env, NOW)
    ingest(env, s1, NOW)
    s2 = snap(env, NOW + timedelta(minutes=5))
    zgh = next(m for m in s2["monitors"] if m["id"] == ZGH)
    g1 = next(g for g in zgh["gates"] if g["id"] == "G1")
    g1.update(status="PASSED", proof="reviewed PR + device test", accepted_on="2026-11-17")
    ingest(env, s2, NOW + timedelta(minutes=5))
    r = rows(env)[-1]
    assert r["gate_delta"] == 1 and r["delta_source"] == "GATE_ACCEPTANCE"
    assert "G1" in r["delta_evidence"]
    m = mm.l2_metrics(rows(env), NOW + timedelta(minutes=6))
    assert m["useful_action_rate"] == 1.0


def test_verified_task_output_is_execution_delta(env):
    ingest(env, snap(env, NOW), NOW)
    t = finish_task(env, NOW + timedelta(minutes=1))
    ingest(env, snap(env, NOW + timedelta(minutes=30)), NOW + timedelta(minutes=30))
    r = rows(env)[-1]
    assert r["execution_delta"] == 1 and r["gate_delta"] == 0
    assert r["delta_source"] == "VERIFIED_TASK_OUTPUT"
    assert f"ht_{t.id}" in r["delta_evidence"]
    # The finished task keeps CLEANUP_PENDING: it is surfaced, not freed.
    assert r["cleanup_state"] == "PENDING"


def test_canary_completion_is_not_useful_work(env):
    ingest(env, snap(env, NOW), NOW)
    finish_task(env, NOW + timedelta(minutes=1), canary=True)
    ingest(env, snap(env, NOW + timedelta(minutes=30)), NOW + timedelta(minutes=30))
    r = rows(env)[-1]
    assert r["delta_source"] == "CANARY"
    m = mm.l2_metrics(rows(env), NOW + timedelta(minutes=31))
    assert m["useful_action_rate"] == 0.0 and m["non_evidence_delta_claims"] == 1


def test_gate_regression_is_flagged_not_counted(env):
    s1 = snap(env, NOW)
    ingest(env, s1, NOW)
    s2 = snap(env, NOW + timedelta(minutes=5))
    zgh = next(m for m in s2["monitors"] if m["id"] == ZGH)
    for g in zgh["gates"]:
        if g["status"] == "PASSED":
            g["status"] = "IN_PROGRESS"
    out = ingest(env, s2, NOW + timedelta(minutes=5))
    assert out[ZGH]["gate_regressed"] is True
    assert rows(env)[-1]["gate_delta"] == 0


# ---------------------------------------- narration excluded / blockers / safety
def test_operator_attested_cycles_never_feed_metrics(env, capsys):
    rc = mm.main(["--db", str(env["ledger"]), "cycle-record", "--json", json.dumps({
        "monitor_id": ZGH, "cycle_at": NOW.isoformat(),
        "readback_status": "VERIFIED_READBACK", "readback_age_seconds": 10,
        "execution_delta": True, "gate_delta": False,
        "delta_source": "VERIFIED_TASK_OUTPUT", "delta_evidence": "automation says done"})])
    assert rc == 0
    capsys.readouterr()
    with mm.ledger(env["ledger"]) as db:
        assert mm.cycles(db, ZGH) == []                     # evidence-only default
        assert len(mm.cycles(db, ZGH, evidence_only=False)) == 1
    rep = mm.meta_report(env["ledger"], None, NOW + timedelta(minutes=1), [ZGH])
    assert rep["monitors"][ZGH]["l2"]["useful_action_rate"] is None
    assert rep["monitors"][ZGH]["attested_cycles_excluded"] == 1


def test_free_text_blocked_gate_is_not_classified(env):
    ingest(env, snap(env, NOW), NOW)
    r = rows(env)[-1]
    assert r["blocker_code"] is None and r["blocker_class"] is None
    out = ingest(env, snap(env, NOW + timedelta(minutes=5)), NOW + timedelta(minutes=5))
    assert out[ZGH]["blocked_gates"] >= 1          # visible, but class stays UNKNOWN


def test_external_owner_blocker_still_blocks_rule_churn_on_ingested_rows(env):
    with mm.ledger(env["ledger"], create=True) as db:
        mm._insert_cycle(db, {
            "monitor_id": ZGH, "cycle_at": NOW.isoformat(),
            "readback_status": "VERIFIED_READBACK", "readback_age_seconds": 30,
            "execution_delta": False, "gate_delta": False,
            "blocker_code": "OWNER_CONSENT"}, NOW, provenance="HARNESS_SNAPSHOT")
        with pytest.raises(mm.MetaMonitorError, match="EXTERNAL_BLOCKER_NO_RULE_CHURN"):
            mm.open_experiment(db, {
                "rule_id": "ZGH-DISPATCH-GUARD-001", "rule_version": "v2",
                "target_monitor": ZGH, "hypothesis": "h",
                "diff": {"retry_guard": {"before": "off", "after": "on"}},
                "prompt_sha256_before": "a" * 64, "prompt_sha256_after": "b" * 64,
                "before_evidence": "ledger", "applied_at": NOW.isoformat()}, NOW)


def test_ingest_never_writes_harness_db(env):
    before = env["hdb"].read_bytes()
    ingest(env, snap(env, NOW), NOW + timedelta(minutes=1))
    ingest(env, snap(env, NOW + timedelta(minutes=5)), NOW + timedelta(minutes=5))
    assert env["hdb"].read_bytes() == before


def test_cli_ingest_to_report_end_to_end(env, capsys):
    path = env["tmp"] / "monitor-progress.json"
    path.write_text(json.dumps(snap(env, datetime.now(timezone.utc))))
    rc = mm.main(["--db", str(env["ledger"]), "ingest", "--snapshot", str(path),
                  "--harness-db", str(env["hdb"]), "--automation-id", ZGH])
    out = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert rc == 0 and out["results"][ZGH]["status"] == "RECORDED"
    assert out["runtime_acceptance"] == "NOT_PROVEN"
    rc = mm.main(["--db", str(env["ledger"]), "report", "--snapshot", str(path),
                  "--monitor", ZGH])
    rep = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert rc == 0 and rep["monitors"][ZGH]["l2"]["cycles"] == 1
    assert rep["monitors"][ZGH]["l2"]["useful_action_rate"] is None   # 1 cycle: no delta yet
    assert rep["runtime_acceptance"] == "NOT_PROVEN"


def test_legacy_ledger_without_new_columns_is_migrated(env):
    legacy = env["tmp"] / "legacy.db"
    legacy.touch(mode=0o600)
    with sqlite3.connect(legacy) as c:
        c.execute("""CREATE TABLE meta_cycle_observations (id INTEGER PRIMARY KEY
            AUTOINCREMENT, monitor_id TEXT NOT NULL, cycle_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL, readback_status TEXT NOT NULL,
            readback_age_seconds INTEGER, execution_delta INTEGER, gate_delta INTEGER,
            delta_source TEXT, delta_evidence TEXT, dispatch_key TEXT,
            duplicate_dispatch INTEGER NOT NULL DEFAULT 0, blocker_code TEXT,
            blocker_class TEXT, blocker_since TEXT, owner_intervention INTEGER,
            cleanup_state TEXT, cleanup_pending_since TEXT, UNIQUE(monitor_id, cycle_at))""")
        c.execute("""INSERT INTO meta_cycle_observations (monitor_id,cycle_at,recorded_at,
            readback_status) VALUES (?,?,?,?)""", (ZGH, NOW.isoformat(), NOW.isoformat(),
                                                   "VERIFIED_READBACK"))
    with mm.ledger(legacy) as db:
        legacy_rows = mm.cycles(db, ZGH, evidence_only=False)
        assert legacy_rows[0]["provenance"] == "OPERATOR_ATTESTED"   # never upgraded
        assert mm.cycles(db, ZGH) == []


def test_service_meta_ingest_is_opt_in_and_non_fatal(env):
    from scripts import harness_monitor_service as svc
    receipt = svc.meta_ingest_step(env["hdb"], env["tmp"] / "absent.json", env["ledger"])
    assert receipt["meta_ingest_healthy"] is False
    assert "absent" in receipt["meta_ingest_error"]
    assert not env["ledger"].exists()
    path = env["tmp"] / "monitor-progress.json"
    path.write_text(json.dumps(snap(env, datetime.now(timezone.utc))))
    ok = svc.meta_ingest_step(env["hdb"], path, env["ledger"])
    assert ok["meta_ingest_healthy"] is True and ok["meta_ingest_recorded"] >= 1
    import inspect
    assert "--meta-ingest" in inspect.getsource(svc.main)   # opt-in flag only


def test_passed_gate_without_proof_is_not_gate_acceptance(env):
    ingest(env, snap(env, NOW), NOW)
    s2 = snap(env, NOW + timedelta(minutes=5))
    zgh = next(m for m in s2["monitors"] if m["id"] == ZGH)
    g1 = next(g for g in zgh["gates"] if g["id"] == "G1")
    g1["status"], g1["proof"] = "PASSED", None
    ingest(env, s2, NOW + timedelta(minutes=5))
    r = rows(env)[-1]
    assert r["gate_delta"] == 1 and r["delta_source"] == "SELF_REPORT"
    assert mm.l2_metrics(rows(env), NOW + timedelta(minutes=6))["useful_action_rate"] == 0.0
