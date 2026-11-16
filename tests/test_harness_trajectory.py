"""Immutable planning baseline, actual history and auditable course corrections."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from app.services.agent_harness_mvp import HarnessError, HarnessStore
from scripts.harness_monitor_bridge import bootstrap, load_registry
from scripts import harness_progress as report
from scripts import harness_trajectory as track

ZGH = "Automation_eec08a5752d08191b3de371065c27320"
START = datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "harness.db"
    HarnessStore(db)
    bootstrap(load_registry(), db)
    return db


def zgh_monitor(db: Path, time: datetime, percent: int | None = None) -> dict:
    snap = report.summarize(db, reference=time)
    zgh = next(x for x in snap["monitors"] if x["id"] == ZGH)
    if percent is not None:
        zgh["completion_percent"] = percent
    return zgh


def test_initial_baseline_is_first_observed_sample_not_invented_history(store):
    sample = zgh_monitor(store, START)
    assert sample["completion_percent"] == 10
    current = track.view(store, sample, START)
    assert current["status"] == "PREVIEW_NO_PERSISTED_BASELINE"
    assert len(current["actual_observations"]) == 1
    assert current["baseline_percent"] == 10
    assert current["baseline_expected_percent"] == 10

    updated = track.observe_all(store, [sample] +
        [m for m in report.summarize(store, reference=START)["monitors"]
         if m["kind"] == "project" and m["id"] != ZGH], START)
    assert updated[ZGH] == 1
    tracked = track.view(store, sample, START)
    assert tracked["status"] == "TRACKED"
    assert tracked["baseline_percent"] == 10
    assert len(tracked["actual_observations"]) == 1
    assert (track.moment(tracked["baseline_target"]) - START).days == 7

    repeat = track.observe_all(store,
        [m for m in report.summarize(store, reference=START)["monitors"]
         if m["kind"] == "project"], START+timedelta(minutes=5))
    assert repeat[ZGH] == 0
    with track.connect(store) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM harness_goal_trajectory_events "
            "WHERE automation_id=? AND kind='PROGRESS'", (ZGH,)
        ).fetchone()[0]
    assert count == 1


def test_flat_real_progress_falls_behind_slope_without_giving_up_baseline(store):
    snap = report.summarize(store, reference=START)
    track.observe_all(store, snap["monitors"], START)
    t = START + timedelta(hours=24)
    current = report.summarize(store, reference=t)
    track.observe_all(store, current["monitors"], t)
    chart = track.view(store, zgh_monitor(store, t), t)
    assert chart["baseline_percent"] == 10
    assert chart["current_percent"] == 10
    assert chart["baseline_expected_percent"] == pytest.approx(22.86, abs=.05)
    assert chart["baseline_deviation_pp"] == pytest.approx(-12.86, abs=.05)
    assert len(chart["actual_observations"]) >= 2
    assert chart["correction_count"] == 0
    assert chart["on_track"] is False


def test_course_correction_preserves_old_line_and_measures_drift_after(store):
    base = report.summarize(store, reference=START)
    track.observe_all(store, base["monitors"], START)
    t1 = START + timedelta(days=1)
    snap = report.summarize(store, reference=t1)
    track.observe_all(store, snap["monitors"], t1)
    zgh = zgh_monitor(store, t1)
    correction = track.record_correction(
        store, zgh, t1, reason="Fix a verified durable cursor gap before dispatch",
        evidence="https://github.com/hungtranbkit/zalo-group-hub/pull/90")
    assert correction["baseline_preserved"]
    assert correction["deviation_before_pp"] == pytest.approx(-12.86, abs=.05)

    t2 = START + timedelta(days=2)
    zgh2 = zgh_monitor(store, t2, percent=20)
    with track.connect(store) as db:
        track._tables(db)
        db.execute("BEGIN IMMEDIATE")
        assert track.observe(db, zgh2, t2, track.plans()[ZGH])
    graph = track.view(store, zgh2, t2)
    assert graph["correction_count"] == 1
    assert graph["baseline_percent"] == 10
    assert graph["current_percent"] == 20
    assert graph["baseline_expected_percent"] == pytest.approx(35.71, abs=.05)
    assert graph["baseline_deviation_pp"] == pytest.approx(-15.71, abs=.05)
    assert graph["adjusted_deviation_pp"] < 0
    pivot = graph["corrections"][0]
    assert pivot["pre_adjustment_gap_pp"] == pytest.approx(-12.86, abs=.05)
    assert pivot["after_adjustment_gap_pp"] == graph["adjusted_deviation_pp"]
    rendered = track.render_svg(graph)
    assert 'trajectory-original-baseline' in rendered
    assert 'trajectory-revised-plan' in rendered
    assert 'trajectory-observed' in rendered
    assert 'aria-label="Đường mục tiêu' in rendered
    assert "↪1" in rendered


def test_direction_change_does_not_change_accepted_gate_percent(store):
    snap = report.summarize(store, reference=START)
    track.observe_all(store, snap["monitors"], START)
    zgh = zgh_monitor(store, START + timedelta(minutes=30))
    before = zgh["completion_percent"]
    with pytest.raises(HarnessError, match="verifiable source"):
        track.record_correction(
            store, zgh, START + timedelta(minutes=30),
            reason="Want to go faster without proof",
            evidence="not-an-evidence-url")
    with pytest.raises(HarnessError, match="reason"):
        track.record_correction(
            store, zgh, START + timedelta(minutes=30),
            reason="oops", evidence="https://github.com/hungtranbkit/zalo-group-hub/pull/90")
    correction = track.record_correction(
        store, zgh, START + timedelta(minutes=30),
        reason="Audit found a gap in exact source-page ordering",
        evidence="https://github.com/hungtranbkit/zalo-group-hub/pull/87",
        next_horizon_hours=72)
    assert correction["new_percent"] == before
    after = report.summarize(store, reference=START+timedelta(hours=1))
    assert next(m for m in after["monitors"] if m["id"]==ZGH)["completion_percent"] == before
    with track.connect(store) as conn:
        baseline = conn.execute("SELECT target_at FROM harness_goal_trajectory_baselines "
                                "WHERE automation_id=?", (ZGH,)).fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM harness_goal_trajectory_events "
                            "WHERE automation_id=? AND kind='CORRECTION'", (ZGH,)).fetchone()[0]
    assert track.moment(baseline)==START+timedelta(days=7)
    assert rows==1


def test_report_has_visual_slope_and_gap_but_project_has_no_24h_deadline(store):
    at = START
    s = report.summarize(store, reference=at)
    track.observe_all(store, s["monitors"], at)
    later = report.summarize(store, reference=at+timedelta(hours=12))
    line = report.markdown(later)
    web = report.html(later)
    assert "↗ Đường thực tế" in line
    assert "mốc THAM CHIẾU" in line
    assert "dự án không có deadline 24h" in line
    assert '<svg' in web
    assert 'trajectory-original-baseline' in web
    assert "Mốc tham chiếu 7 ngày" in web
    assert "10%" in web
    assert "không có task nào đang chiếm wip" in web.lower()


def test_nonexistent_plan_or_late_adjustment_fails_closed(store):
    snap = report.summarize(store, reference=START)
    track.observe_all(store, snap["monitors"], START)
    with pytest.raises(HarnessError):
        track.record_correction(
            store, zgh_monitor(store, START), START-timedelta(days=1),
            reason="Trying to rewrite earlier trajectory events",
            evidence="https://github.com/hungtranbkit/zalo-group-hub/pull/87")
    with pytest.raises(HarnessError, match="horizon"):
        track.record_correction(
            store, zgh_monitor(store, START), START+timedelta(minutes=2),
            reason="Try an absurd new planning horizon",
            evidence="https://github.com/hungtranbkit/zalo-group-hub/pull/87",
            next_horizon_hours=2000)


def test_same_goal_plans_do_not_apply_to_continuous_cleanup(store):
    snap = report.summarize(store,reference=START)
    maintenance = next(x for x in snap["monitors"] if x["kind"]=="maintenance")
    assert track.view(store,maintenance,START) is None
    with pytest.raises(HarnessError,match="ongoing maintenance"):
        track.record_correction(store,maintenance,START,
            reason="Do not invent a 100 percent target for cleanup",
            evidence="https://github.com/hungtranbkit/projectflow-workspace-manager/pull/48")
