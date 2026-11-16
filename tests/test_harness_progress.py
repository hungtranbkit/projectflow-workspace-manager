"""Evidence-weighted gate and immutable TTL progress regression tests."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from app.services.agent_harness_mvp import HarnessError, HarnessStore
from scripts.harness_monitor_bridge import bootstrap, load_registry
from scripts import harness_progress as progress


NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
ZGH = "Automation_eec08a5752d08191b3de371065c27320"


@pytest.fixture
def runtime(tmp_path):
    db = tmp_path / "harness.db"
    HarnessStore(db)
    registry = load_registry()
    assert bootstrap(registry, db)["total"] == 25
    return db, registry


def test_all_enabled_monitors_have_explicit_gate_policies(runtime):
    db, registry = runtime
    result = progress.summarize(db, reference=NOW)
    assert result["monitor_count"] == 4
    assert result["paused_count"] == 21
    zgh = next(x for x in result["monitors"] if x["id"] == ZGH)
    assert zgh["completion_percent"] == 10
    assert zgh["gates_passed"] == 1
    assert zgh["gates_total"] == 6
    assert zgh["goal_has_deadline"] is False
    assert "business" not in zgh["progress_metric"]
    assert zgh["wip"] == 0
    cleanup = next(x for x in result["monitors"] if x["kind"] == "maintenance")
    assert cleanup["completion_percent"] is None


def test_progress_is_not_inflated_by_agent_canary_done(runtime):
    db, registry = runtime
    store = HarnessStore(db)
    goal = registry[ZGH]["main_goal"]
    start = NOW - timedelta(minutes=12)
    task = store.create(
        key="read-only-ZGH-canary",
        main_goal=goal, child_goal="CANARY ONLY: read-only Codex handoff",
        dod="connectivity proof", agent="codex", node="dell-linux",
        created_at=start, ttl=timedelta(hours=24))
    store.fail(task.id, at=start + timedelta(minutes=5),
               reason="technical canary not accepted as business proof")
    store.cleanup(task.id, at=start + timedelta(minutes=6),
                  executor_stopped=True, no_active_refs=True,
                  dirty_worktree=False, evidence_preserved=True,
                  deletion_verified=True)
    zgh = next(x for x in progress.summarize(
        db, reference=NOW)["monitors"] if x["id"] == ZGH)
    assert zgh["completion_percent"] == 10
    assert zgh["wip"] == 0
    assert zgh["child_failed"] == 1
    recent = zgh["recent_tasks"][0]
    assert recent["project_DoD"] == "NOT_TESTED_BY_CANARY"
    assert recent["remaining_seconds"] is None
    assert recent["finished"] is True
    # Completed tasks must not keep burning their original 24h timer forever.
    assert recent["time_used_percent"] < 3


def test_24h_budget_reports_elapsed_remaining_and_overdue(runtime):
    db, registry = runtime
    store = HarnessStore(db)
    start = NOW - timedelta(hours=6)
    t = store.create(
        key="zgh-real-work", main_goal=registry[ZGH]["main_goal"],
        child_goal="Repair a specific verified V229 blocker",
        dod="source-coverage proof and independent unit test", agent="codex",
        node="dell-linux", created_at=start, ttl=timedelta(hours=24))
    snap = progress.summarize(db, reference=NOW)
    monitor = next(x for x in snap["monitors"] if x["id"] == ZGH)
    assert monitor["wip"] == 1
    clock = monitor["active_tasks"][0]
    assert clock["id"] == t.id
    assert clock["ttl_seconds"] == 86400
    assert clock["time_used_percent"] == 25
    assert clock["remaining_seconds"] == 18 * 3600
    assert clock["overdue"] is False
    assert clock["remaining_label"] == "18h 00m"
    assert clock["time_used_bar"].count("█") == 3
    past = progress.task_clock(t, NOW + timedelta(hours=19))
    assert past["overdue"] is True
    assert past["time_used_percent"] == 100
    assert past["remaining_seconds"] == 0
    assert progress.bar(0) == "░" * 12
    assert progress.bar(100) == "█" * 12


def test_pass_must_have_reviewed_proof_and_weight_sum(tmp_path):
    current = json.loads(progress.DEFAULT_GATES.read_text(encoding="utf-8"))
    current["goals"][0]["gates"][0].pop("proof")
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(current))
    with pytest.raises(HarnessError, match="acceptance proof"):
        progress.read_gates(path)
    current = json.loads(progress.DEFAULT_GATES.read_text(encoding="utf-8"))
    current["goals"][0]["gates"][0]["weight"] = 11
    path.write_text(json.dumps(current))
    with pytest.raises(HarnessError, match="sum to 100"):
        progress.read_gates(path)


def test_html_dashboard_is_mobile_and_private_and_not_faked(runtime):
    db, _ = runtime
    snapshot = progress.summarize(db, reference=NOW)
    out = progress.write_private_dashboard(snapshot, db.parent)
    h = Path(out["html"])
    j = Path(out["json"])
    assert h.is_file() and j.is_file()
    assert h.stat().st_mode & 0o777 == 0o600
    markup = h.read_text(encoding="utf-8")
    assert 'name="viewport"' in markup
    assert 'role="progressbar"' in markup
    assert "Zalo Hub V2/V229" in markup
    assert "10%" in markup
    assert "Không tính PR nháp" in markup
    assert json.loads(j.read_text())["health"]["status"] == "UNKNOWN"


def test_health_must_be_fresh_to_count_as_healthy(runtime):
    db, _ = runtime
    record = {"observed_at": (NOW - timedelta(minutes=20)).isoformat(),
              "healthy": True, "scheduler_healthy": True, "bridge_healthy": True}
    (db.parent / "monitor-health.json").write_text(json.dumps(record))
    snap = progress.summarize(db, reference=NOW)
    assert snap["health"]["status"] == "FAILED_OR_STALE"
    record["observed_at"] = (NOW - timedelta(minutes=5)).isoformat()
    (db.parent / "monitor-health.json").write_text(json.dumps(record))
    snap = progress.summarize(db, reference=NOW)
    assert snap["health"]["status"] == "HEALTHY"


def test_report_includes_labeled_project_and_child_timers(runtime):
    db, registry = runtime
    store = HarnessStore(db)
    store.create(
        key="time-test", main_goal=registry[ZGH]["main_goal"],
        child_goal="G1 missing text blocker", dod="one test",
        agent="codex", node="dell-linux",
        created_at=NOW - timedelta(hours=3), ttl=timedelta(hours=24))
    text = progress.markdown(progress.summarize(db, reference=NOW))
    assert "gate đã nghiệm thu" in text
    assert "10%" in text
    assert "13%" in text or "12%" in text
    assert "còn 21h 00m" in text
    assert "dự án không có deadline 24h" in text


def test_finished_task_still_has_a_time_bar_without_negative_remaining(runtime):
    db, registry = runtime
    store = HarnessStore(db)
    origin = NOW - timedelta(minutes=10)
    task = store.create(key="completed-time-bar", main_goal=registry[ZGH]["main_goal"],
                        child_goal="CANARY ONLY: ended task with time evidence",
                        dod="one technical readback", agent="codex", node="dell-linux",
                        created_at=origin, ttl=timedelta(hours=2))
    store.fail(task.id, at=origin + timedelta(minutes=3), reason="ended task")
    store.cleanup(task.id, at=origin + timedelta(minutes=4),
                  executor_stopped=True, no_active_refs=True,
                  dirty_worktree=False, evidence_preserved=True,
                  deletion_verified=True)
    snap = progress.summarize(db, reference=NOW)
    html = progress.html(snap)
    assert "Task gần nhất" in html
    assert "đã kết thúc; không còn đồng hồ đếm ngược" in html
    assert f"#{task.id}" in html
