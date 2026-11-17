"""Scheduled monitor compact readback must be truthful and fail closed."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from app.services.agent_harness_mvp import HarnessStore
from scripts.harness_monitor_bridge import bootstrap, load_registry
from scripts.harness_progress import summarize
from scripts.harness_monitor_report import percent_bar, report

NOW = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)
ZGH = "Automation_eec08a5752d08191b3de371065c27320"
CLEANUP = "6af907fc44a0819198d7dc0ddbc9249a"


@pytest.fixture
def snapshot(tmp_path):
    db = tmp_path / "test.db"
    HarnessStore(db)
    bootstrap(load_registry(), db)
    raw = summarize(db, reference=NOW)
    # summarize cannot mark healthy until scheduler evidence is present.
    raw["health"] = {"status": "HEALTHY"}
    return raw, db


def test_compact_readback_verified_zgh_only(snapshot):
    raw, _ = snapshot
    x = report(raw, ZGH, NOW + timedelta(minutes=1))
    assert x["status"] == "VERIFIED_READBACK"
    assert x["completion_percent"] == 10
    assert x["project_business_accepted"] is False
    assert x["completion_bar"] == "█" + "░" * 11
    assert x["gates_passed"] == 1
    assert x["project_has_24h_deadline"] is False
    assert x["chart"]["original_plan"][0]["percent"] == 10
    assert x["chart"]["actual"][0]["percent"] == 10
    assert x["chart"]["revised_plan"] is None
    assert x["chart"]["planning_horizon_provisional"] is True
    assert x["wip"] == 0


def test_stale_or_unhealthy_does_not_report_last_green_percent(snapshot):
    raw, _ = snapshot
    stale = report(raw, ZGH, NOW + timedelta(minutes=16))
    assert stale == {"status": "HARNESS_UNKNOWN",
                     "automation_id": ZGH,
                     "reason": "STALE_OR_UNHEALTHY", "age_seconds": 960}
    raw["health"]["status"] = "FAILED_OR_STALE"
    fail = report(raw, ZGH, NOW)
    assert fail["status"] == "HARNESS_UNKNOWN"
    assert "completion_percent" not in fail
    assert "chart" not in fail


def test_task_deadline_is_calculated_at_readback_time(snapshot):
    raw, db = snapshot
    registry = load_registry()
    created = NOW - timedelta(hours=6)
    task = HarnessStore(db).create(
        key="monitor-report-now", main_goal=registry[ZGH]["main_goal"],
        child_goal="One protected atomic task", dod="independent test",
        agent="codex", node="dell-linux", created_at=created,
        ttl=timedelta(hours=24))
    raw = summarize(db, reference=NOW)
    raw["health"] = {"status": "HEALTHY"}
    result = report(raw, ZGH, NOW + timedelta(minutes=5))
    assert result["task_time_budget"][0]["id"] == task.id
    assert result["task_time_budget"][0]["remaining_seconds"] == 17*3600+55*60
    assert result["task_time_budget"][0]["time_used_percent"] == 25
    assert result["task_time_budget"][0]["overdue"] is False
    overdue = report(raw, ZGH, NOW + timedelta(hours=18, minutes=1),
                     max_age_seconds=100000)
    assert overdue["task_time_budget"][0]["overdue"] is True
    assert overdue["task_time_budget"][0]["remaining_seconds"] == 0


def test_maintenance_has_no_fabricated_percentage_or_diagonal(snapshot):
    raw, _ = snapshot
    x = report(raw, CLEANUP, NOW)
    assert x["status"] == "VERIFIED_READBACK"
    assert x["completion_percent"] is None
    assert x["completion_bar"] is None
    assert x["chart"] is None
    assert x["project_business_accepted"] is None


def test_nonexistent_id_does_not_leak_other_monitor_data(snapshot):
    raw, _ = snapshot
    x = report(raw, "not-an-active-monitor", NOW)
    assert x["status"] == "HARNESS_UNKNOWN"
    assert x["reason"] == "MONITOR_NOT_FOUND_OR_PAUSED"
    assert "completion_percent" not in x


def test_history_is_not_backfilled_with_fake_points(snapshot):
    raw, _ = snapshot
    x = report(raw, ZGH, NOW)
    assert len(x["chart"]["actual"]) == 1
    assert len(x["chart"]["original_plan"]) == 2
    assert x["chart"]["baseline_deviation_pp"] == 0


def test_invalid_percent_or_deadline_fails_closed(snapshot):
    raw, _ = snapshot
    raw["monitors"] = [dict(raw["monitors"][-1])]
    raw["monitors"][0]["completion_percent"] = 101
    with pytest.raises(ValueError, match="invalid gate percent"):
        report(raw, ZGH, NOW)
    assert percent_bar(0) == "░"*12
    assert percent_bar(100) == "█"*12
    with pytest.raises(ValueError):
        percent_bar(-1)



def test_terminal_mcp_payload_is_bounded_and_parses(snapshot):
    from scripts.harness_monitor_report import terminal_compact
    raw, _ = snapshot
    full = report(raw, ZGH, NOW)
    compact = terminal_compact(full)
    text = "HARNESS_REPORT=" + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    assert len(text.encode("utf-8")) < 700
    assert json.loads(text.split("=", 1)[1])["status"] == "VERIFIED_READBACK"
    assert compact["pct"] == 10
    assert compact["bar"] == "█" + "░"*11
    assert compact["gates"] == "1/6"
    assert compact["pivots"] == 0
    assert compact["original"][0][1] == 10
    assert len(compact["actual"]) == 1
    assert compact["tasks"] == []


def test_terminal_payload_with_two_active_tasks_still_fits_tail(snapshot):
    from scripts.harness_monitor_report import terminal_compact
    raw, db = snapshot
    store = HarnessStore(db)
    goal = load_registry()[ZGH]["main_goal"]
    for number in (1,2):
        store.create(key=f"monitor-tiny-{number}",main_goal=goal,
                     child_goal=f"atomic task {number}",dod="test",
                     agent="codex",node="dell-linux",
                     created_at=NOW-timedelta(hours=number),
                     ttl=timedelta(hours=24))
    updated = summarize(db,reference=NOW)
    updated["health"] = {"status":"HEALTHY"}
    compact = terminal_compact(report(updated,ZGH,NOW))
    packed = "HARNESS_REPORT="+json.dumps(compact,ensure_ascii=False,separators=(",",":"))
    assert len(packed.encode("utf-8")) < 950
    assert len(compact["tasks"]) == 2
    assert compact["tasks"][0]["remaining_min"] == 23 * 60
    assert compact["tasks"][1]["remaining_min"] == 22 * 60


def test_compact_failed_readback_does_not_expose_stale_gate_pct(snapshot):
    from scripts.harness_monitor_report import terminal_compact
    raw, _ = snapshot
    compact = terminal_compact(report(raw,ZGH,NOW+timedelta(minutes=30)))
    assert compact["status"] == "HARNESS_UNKNOWN"
    assert compact["reason"] == "STALE_OR_UNHEALTHY"
    assert "pct" not in compact
    assert "actual" not in compact
