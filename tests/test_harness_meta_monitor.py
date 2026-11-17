"""Issue #52 acceptance: Meta-Monitor L1/L2/L3, experiments, convergence.

Every test uses its own tmp ledger; none touches the production Harness DB,
automation prompts, schedules or GitHub.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import sqlite3
import stat

import pytest

from app.services.agent_harness_mvp import HarnessStore
from scripts import harness_meta_monitor as mm
from scripts.harness_monitor_bridge import bootstrap, load_registry
from scripts.harness_progress import summarize

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
MON = "6af698c9a5308191899c6a7e013cee5f"
ZGH = "Automation_eec08a5752d08191b3de371065c27320"
SHA_A = sha256(b"prompt-v1").hexdigest()
SHA_B = sha256(b"prompt-v2").hexdigest()


@pytest.fixture
def db(tmp_path):
    with mm.ledger(tmp_path / "meta.db", create=True) as conn:
        yield conn


def cyc(db, at, *, status="VERIFIED_READBACK", age=60, exe=False, gate=False,
        source=None, evidence=None, monitor=MON, **extra):
    # Metric-math fixtures stand in for Harness-derived (ingested) cycles.
    return mm._insert_cycle(db, {
        "monitor_id": monitor, "cycle_at": at.isoformat(),
        "readback_status": status, "readback_age_seconds": age,
        "execution_delta": exe, "gate_delta": gate, "delta_source": source,
        "delta_evidence": evidence, **extra}, at + timedelta(minutes=1),
        provenance="HARNESS_SNAPSHOT")


def useful(db, at, **kw):
    return cyc(db, at, exe=True, source="VERIFIED_TASK_OUTPUT",
               evidence=f"harness task ht_{int(at.timestamp())} DONE_VERIFIED", **kw)


def spec(**over):
    s = {"rule_id": "stall-diagnose", "rule_version": "v2", "target_monitor": MON,
         "hypothesis": "Diagnosing after 2 flat cycles raises useful action rate",
         "diff": {"diagnose_after_cycles": {"before": "3", "after": "2"}},
         "prompt_sha256_before": SHA_A, "prompt_sha256_after": SHA_B,
         "before_evidence": "cycles 2026-10-25..11-01 in meta ledger",
         "applied_at": T0.isoformat()}
    s.update(over)
    return s


# ------------------------------------------------------------ ledger safety
def test_ledger_is_owner_only_and_append_only(tmp_path, db):
    path = tmp_path / "meta.db"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cyc(db, T0)
    db.commit()
    for sql in ("UPDATE meta_cycle_observations SET gate_delta=1",
                "DELETE FROM meta_cycle_observations"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(sql)


def test_group_readable_ledger_refused(tmp_path):
    path = tmp_path / "open.db"
    path.touch()
    os.chmod(path, 0o644)
    with pytest.raises(mm.MetaMonitorError, match="permissions"):
        with mm.ledger(path):
            pass


def test_secret_like_text_refused(db):
    with pytest.raises(mm.MetaMonitorError, match="secret"):
        mm.open_experiment(db, spec(hypothesis="token=ghp_" + "a" * 30), T0)


# --------------------------------------------------- L2: UNKNOWN not zero
def test_absent_observations_are_unknown_not_zero(db):
    m = mm.l2_metrics(mm.cycles(db, MON), T0)
    assert m["cycles"] == 0
    for key in ("useful_action_rate", "stale_readback_rate", "no_delta_cycles",
                "owner_interventions", "duplicate_dispatch_count",
                "cleanup_pending_age_seconds"):
        assert m[key] is None
    assert mm.next_step(m)["action"] == "UNKNOWN"


def test_stale_readback_is_not_useful_and_not_zero_delta(db):
    for i in range(3):
        useful(db, T0 + timedelta(hours=i), age=5000)  # stale readback
    m = mm.l2_metrics(mm.cycles(db, MON), T0 + timedelta(hours=3))
    assert m["stale_readback_rate"] == 1.0
    assert m["useful_action_rate"] is None
    assert m["no_delta_cycles"] is None


def test_cron_commit_canary_wip_never_count_as_useful(db):
    for i, src in enumerate(["CRON_COMPLETED", "COMMIT", "CANARY", "HARNESS_WIP"]):
        cyc(db, T0 + timedelta(hours=i), exe=True, source=src, evidence="x")
    m = mm.l2_metrics(mm.cycles(db, MON), T0 + timedelta(hours=5))
    assert m["useful_action_rate"] == 0.0
    assert m["non_evidence_delta_claims"] == 4


def test_independent_delta_needs_evidence_ref(db):
    with pytest.raises(mm.MetaMonitorError, match="delta_evidence"):
        cyc(db, T0, gate=True, source="GATE_ACCEPTANCE")


def test_duplicate_task_receipt_is_not_useful_work(db):
    useful(db, T0, dispatch_key="dt_123")
    dup = useful(db, T0 + timedelta(hours=1), dispatch_key="dt_123")
    assert dup["duplicate_dispatch"] is True
    m = mm.l2_metrics(mm.cycles(db, MON), T0 + timedelta(hours=2))
    assert m["duplicate_dispatch_count"] == 1
    assert m["useful_actions"] == 1 and m["useful_action_rate"] == 0.5


def test_cleanup_pending_keeps_age_and_does_not_free_wip(db):
    cyc(db, T0, cleanup_state="PENDING",
        cleanup_pending_since=(T0 - timedelta(hours=5)).isoformat())
    m = mm.l2_metrics(mm.cycles(db, MON), T0 + timedelta(hours=1))
    assert m["cleanup_pending_age_seconds"] == 6 * 3600
    with pytest.raises(mm.MetaMonitorError):
        cyc(db, T0 + timedelta(hours=2), cleanup_state="DONE")


def test_ledger_never_touches_harness_db_or_child_invariants(tmp_path, db):
    hdb = tmp_path / "harness.db"
    HarnessStore(hdb)
    bootstrap(load_registry(), hdb)
    before = hdb.read_bytes()
    useful(db, T0)
    mm.open_experiment(db, spec(applied_at=(T0 + timedelta(minutes=5)).isoformat()),
                       T0 + timedelta(minutes=10))
    assert hdb.read_bytes() == before


# ------------------------------------- blockers: external vs orchestration
def test_external_owner_blocker_holds_and_blocks_rule_churn(db):
    for i in range(4):
        cyc(db, T0 + timedelta(hours=i), blocker_code="OWNER_CONSENT",
            blocker_since=T0.isoformat())
    m = mm.l2_metrics(mm.cycles(db, MON), T0 + timedelta(hours=4))
    assert m["no_delta_cycles"] == 4
    assert m["blocker"] == {"code": "OWNER_CONSENT", "class": "EXTERNAL_OWNER",
                            "age_seconds": 4 * 3600}
    assert mm.next_step(m)["action"] == "HOLD_OWNER_ACTION"
    with pytest.raises(mm.MetaMonitorError, match="EXTERNAL_BLOCKER_NO_RULE_CHURN"):
        mm.open_experiment(db, spec(applied_at=(T0 + timedelta(hours=4)).isoformat()),
                           T0 + timedelta(hours=4))


def test_product_bug_not_labelled_orchestration(db):
    r = cyc(db, T0, blocker_code="PRODUCT_TEST_FAILURE")
    assert r["blocker_class"] == "PRODUCT_DEFECT"
    m = mm.l2_metrics(mm.cycles(db, MON), T0 + timedelta(hours=1))
    assert mm.next_step(m)["action"] == "FIX_PRODUCT"
    unknown = cyc(db, T0 + timedelta(hours=1), blocker_code="SOMETHING_NEW")
    assert unknown["blocker_class"] == "UNKNOWN"


def test_orchestration_stall_escalates_diagnose_then_pivot(db):
    for i in range(2):
        cyc(db, T0 + timedelta(hours=i), blocker_code="STALE_READBACK")
    m = mm.l2_metrics(mm.cycles(db, MON), T0 + timedelta(hours=2))
    assert mm.next_step(m)["action"] == "DIAGNOSE"
    cyc(db, T0 + timedelta(hours=2), blocker_code="STALE_READBACK")
    m = mm.l2_metrics(mm.cycles(db, MON), T0 + timedelta(hours=3))
    assert mm.next_step(m)["action"] == "PIVOT"


# --------------------------------------------------------- L3 experiments
def _baseline(db, rate_useful: int, n: int = 4):
    for i in range(n):
        at = T0 - timedelta(hours=n - i)
        (useful if i < rate_useful else cyc)(db, at)


def test_experiment_requires_single_variable_and_distinct_digests(db):
    with pytest.raises(mm.MetaMonitorError, match="ONE variable"):
        mm.open_experiment(db, spec(diff={"a": {"before": "1", "after": "2"},
                                          "b": {"before": "1", "after": "2"}}), T0)
    with pytest.raises(mm.MetaMonitorError, match="digests"):
        mm.open_experiment(db, spec(prompt_sha256_after=SHA_A), T0)


def test_one_open_experiment_per_monitor_and_versions_immutable(db):
    mm.open_experiment(db, spec(), T0)
    with pytest.raises(mm.MetaMonitorError, match="still open"):
        mm.open_experiment(db, spec(rule_version="v3"), T0)


def test_two_new_observations_required_and_no_fabricated_checkpoints(db):
    _baseline(db, rate_useful=1)
    mm.open_experiment(db, spec(), T0)
    with pytest.raises(mm.MetaMonitorError, match="NO_NEW_CYCLE_EVIDENCE"):
        mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=25), "24h")
    useful(db, T0 + timedelta(hours=1))
    with pytest.raises(mm.MetaMonitorError, match="not reached"):
        mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=2), "24h")
    mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=24), "24h")
    ev = mm.evaluate_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=80))
    assert ev["status"] == "AWAITING_OBSERVATIONS"
    assert ev["checkpoints"] == {"24h": "SAMPLED", "72h": "OVERDUE_NO_SAMPLE",
                                 "7d": "NOT_DUE"}
    with pytest.raises(mm.MetaMonitorError, match="cannot judge KEEP"):
        mm.record_verdict(db, "stall-diagnose@v2", "KEEP", "looks good",
                          T0 + timedelta(hours=80))
    # Same cycle evidence cannot be re-sampled as a second observation.
    with pytest.raises(mm.MetaMonitorError, match="NO_NEW_CYCLE_EVIDENCE"):
        mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=73), "72h")


def test_keep_when_both_new_observations_improve(db):
    _baseline(db, rate_useful=1)               # before rate 0.25
    mm.open_experiment(db, spec(), T0)
    useful(db, T0 + timedelta(hours=1))
    mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=24), "24h")
    useful(db, T0 + timedelta(hours=30))
    mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=72), "72h")
    ev = mm.evaluate_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=72))
    assert ev["recommendation"] == "KEEP"
    with pytest.raises(mm.MetaMonitorError, match="recommends KEEP"):
        mm.record_verdict(db, "stall-diagnose@v2", "REVERT", "x", T0 + timedelta(hours=72))
    mm.record_verdict(db, "stall-diagnose@v2", "KEEP", "2 obs improved",
                      T0 + timedelta(hours=72))
    with pytest.raises(mm.MetaMonitorError, match="already judged"):
        mm.record_verdict(db, "stall-diagnose@v2", "INCONCLUSIVE", "x",
                          T0 + timedelta(hours=73))
    assert mm.l3_summary(db, T0 + timedelta(hours=73))["verdicts"]["KEEP"] == 1


def test_unknown_baseline_is_inconclusive_not_improvement(db):
    mm.open_experiment(db, spec(), T0)          # no before cycles at all
    useful(db, T0 + timedelta(hours=1))
    mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=2))
    useful(db, T0 + timedelta(hours=3))
    mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=4))
    ev = mm.evaluate_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=4))
    assert ev["before_useful_action_rate"] is None
    assert ev["recommendation"] == "INCONCLUSIVE"


def _regressed(db):
    _baseline(db, rate_useful=4)               # before rate 1.0
    mm.open_experiment(db, spec(), T0)
    cyc(db, T0 + timedelta(hours=1))
    mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=2))
    cyc(db, T0 + timedelta(hours=3))
    mm.observe_experiment(db, "stall-diagnose@v2", T0 + timedelta(hours=4))


def test_regression_proposes_rollback_but_never_over_concurrent_edit(db):
    _regressed(db)
    now = T0 + timedelta(hours=5)
    assert mm.evaluate_experiment(db, "stall-diagnose@v2", now)["recommendation"] == "REVERT"
    assert mm.rollback_proposal(db, "stall-diagnose@v2", None, now)["action"] == "UNKNOWN"
    other = sha256(b"someone else edited").hexdigest()
    clash = mm.rollback_proposal(db, "stall-diagnose@v2", other, now)
    assert clash["action"] == "CONCURRENT_EDIT_DETECTED"
    ok = mm.rollback_proposal(db, "stall-diagnose@v2", SHA_B, now)
    assert ok["action"] == "PROPOSE_ROLLBACK"
    assert ok["restore_value"] == "3" and ok["applies_automatically"] is False


# ------------------------------------------------------------ convergence
def _quiet_week(db, now):
    at = now - timedelta(days=8)
    while at <= now - timedelta(hours=1):
        useful(db, at)
        at += timedelta(hours=12)


def _guard(db, now, code=0):
    mm.record_guard(db, "ci-test-fast", "8bcff05bb904", code, "40 passed", now)


def test_stable_maintenance_requires_evidence_guard_and_quiet_week(db):
    now = T0 + timedelta(days=10)
    assert mm.convergence(db, now)["state"] == "UNKNOWN"
    _quiet_week(db, now)
    state = mm.convergence(db, now)
    assert state["state"] == "ACTIVE_IMPROVEMENT"
    assert "GUARD_EVIDENCE_ABSENT" in state["reasons"]
    _guard(db, now - timedelta(hours=1), code=1)
    assert mm.convergence(db, now)["state"] == "ACTIVE_IMPROVEMENT"
    _guard(db, now - timedelta(minutes=30))
    assert mm.convergence(db, now)["state"] == "STABLE_MAINTENANCE"
    stale = mm.convergence(db, now + timedelta(hours=30))
    assert stale["state"] == "UNKNOWN"          # no fresh cycle in 24h


def test_recent_orchestration_defect_or_serious_open_blocks_convergence(db):
    now = T0 + timedelta(days=10)
    _quiet_week(db, now)
    _guard(db, now)
    mm.record_defect(db, {"defect_id": "dup-dispatch-1", "event": "OPEN",
                          "kind": "ORCHESTRATION_DEFECT", "severity": "MINOR",
                          "verified": True, "evidence": "cycle 42 duplicate",
                          "at": (now - timedelta(days=2)).isoformat()}, now)
    assert mm.convergence(db, now)["state"] == "ACTIVE_IMPROVEMENT"


def test_verified_regression_leaves_stable_state(db):
    now = T0 + timedelta(days=10)
    _quiet_week(db, now)
    _guard(db, now)
    assert mm.convergence(db, now)["state"] == "STABLE_MAINTENANCE"
    mm.record_defect(db, {"defect_id": "reg-1", "event": "OPEN", "kind": "REGRESSION",
                          "severity": "MINOR", "verified": False,
                          "evidence": "unconfirmed report"}, now)
    claim = mm.convergence(db, now)
    assert claim["state"] == "STABLE_MAINTENANCE"
    assert claim["unverified_regression_claims"] == ["reg-1"]
    mm.record_defect(db, {"defect_id": "reg-2", "event": "OPEN", "kind": "REGRESSION",
                          "severity": "SERIOUS", "verified": True,
                          "evidence": "gate test failed on 3 cycles"}, now)
    assert mm.convergence(db, now)["state"] == "REGRESSION_ACTIVE"
    mm.record_defect(db, {"defect_id": "reg-2", "event": "RESOLVED", "kind": "REGRESSION",
                          "severity": "SERIOUS", "verified": True,
                          "evidence": "fix verified"}, now)
    assert mm.convergence(db, now)["state"] == "STABLE_MAINTENANCE"


# --------------------------------------------------------- L1 + report CLI
def test_l1_uses_fresh_gate_checkpoint_only(tmp_path):
    hdb = tmp_path / "harness.db"
    HarnessStore(hdb)
    bootstrap(load_registry(), hdb)
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    snap = summarize(hdb, reference=now)
    snap["health"] = {"status": "HEALTHY"}
    fresh = mm.l1_outcome(snap, ZGH, now + timedelta(minutes=1))
    assert fresh["status"] == "MEASURED" and fresh["completion_percent"] == 10
    assert fresh["business_accepted"] is False
    assert fresh["gate_delta_pp_7d"] is None    # one point is not a trend
    assert mm.l1_outcome(snap, ZGH, now + timedelta(hours=1))["status"] == "UNKNOWN"
    assert mm.l1_outcome(None, ZGH, now)["status"] == "UNKNOWN"


def test_report_cli_is_honest_without_ledger(tmp_path, capsys):
    code = mm.main(["--db", str(tmp_path / "none.db"), "report",
                    "--snapshot", str(tmp_path / "absent.json")])
    out = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert code == 0 and out["status"] == "UNKNOWN"
    assert out["runtime_acceptance"] == "NOT_PROVEN"
    assert out["enforcement"] == "LEDGER_ONLY_NOT_BACKEND_ENFORCED"
    assert not (tmp_path / "none.db").exists()


def test_cli_refuses_bad_input_with_exit_2(tmp_path, capsys):
    code = mm.main(["--db", str(tmp_path / "m.db"), "cycle-record", "--json",
                    json.dumps({"monitor_id": MON, "cycle_at": "2026-11-01T00:00:00",
                                "readback_status": "VERIFIED_READBACK"})])
    assert code == 2
    assert "REFUSED" in capsys.readouterr().out
