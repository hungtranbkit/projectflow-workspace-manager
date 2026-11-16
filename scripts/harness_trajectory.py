"""Append-only goal trajectory and course-correction evidence.

Baseline: the first observed, accepted gate percentage to 100% over an
explicitly PROVISIONAL seven-day planning window. The original diagonal is
immutable, never reset. Corrections are signed-off direction changes with a
source reference and are overlaid as a new forward-looking slope.

The graph does NOT predict project delivery, claim a project deadline, or
convert a technical agent PASS into a business acceptance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.agent_harness_mvp import HarnessError
from scripts.harness_monitor_bridge import connect, STATE_DB

CONFIG = Path(__file__).resolve().parents[1] / "config/harness_trajectory_plans.json"
BASELINE_TABLE = "harness_goal_trajectory_baselines"
EVENT_TABLE = "harness_goal_trajectory_events"
WINDOW_MIN = timedelta(minutes=5)


def moment(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise HarnessError("trajectory timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise HarnessError("timezone required")
    return dt.astimezone(timezone.utc).isoformat()


def bounded(value: float) -> float:
    return max(0.0, min(100.0, value))


def linear(a: datetime, b: datetime, first: float, second: float,
           at: datetime) -> float:
    span = (b - a).total_seconds()
    if span <= 0:
        raise HarnessError("invalid planning horizon")
    share = (at - a).total_seconds() / span
    return bounded(first + (second - first) * share)


def plans(path: Path = CONFIG) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("schema_version") != 1 or data.get("status") !=
            "PROVISIONAL_PLANNING_BASELINE"):
        raise HarnessError("unreviewed trajectory plan")
    result = {}
    for row in data.get("projects", []):
        key = row.get("automation_id")
        hours = row.get("horizon_hours")
        if (not isinstance(key, str) or key in result
                or type(hours) is not int or not 24 <= hours <= 24 * 30
                or row.get("provisional") is not True or not row.get("source")):
            raise HarnessError("invalid or duplicate trajectory plan")
        result[key] = row
    if not result:
        raise HarnessError("missing trajectory plans")
    return result


def _tables(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {BASELINE_TABLE} (
            automation_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            target_at TEXT NOT NULL,
            initial_percent INTEGER NOT NULL CHECK(initial_percent BETWEEN 0 AND 100),
            planning_horizon_hours INTEGER NOT NULL,
            provisional INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {EVENT_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            automation_id TEXT NOT NULL REFERENCES {BASELINE_TABLE}(automation_id),
            kind TEXT NOT NULL CHECK(kind IN ('PROGRESS', 'CORRECTION')),
            observed_at TEXT NOT NULL,
            actual_percent INTEGER NOT NULL CHECK(actual_percent BETWEEN 0 AND 100),
            policy_digest TEXT NOT NULL,
            reason TEXT,
            evidence TEXT,
            adjusted_target_at TEXT,
            old_plan_deviation_pp REAL,
            UNIQUE(automation_id,kind,observed_at)
        );
        CREATE INDEX IF NOT EXISTS idx_harness_trajectory_events
            ON {EVENT_TABLE}(automation_id,observed_at,id);
    """)


def _digest(monitor: dict[str, Any]) -> str:
    accepted = sorted((
        (g["id"], g["weight"], g.get("proof"), g.get("accepted_at"),
         g.get("accepted_on"))
        for g in monitor.get("gates", []) if g["status"] == "PASSED"
    ))
    return sha256(json.dumps(accepted, ensure_ascii=False).encode()).hexdigest()


def observe(
    conn: sqlite3.Connection, monitor: dict[str, Any], at: datetime,
    plan: dict[str, Any],
) -> bool:
    """Append a checkpoint at most every 30m (always on actual gate change)."""
    key = monitor["id"]
    pct = monitor["completion_percent"]
    if pct is None or type(pct) is not int or not 0 <= pct <= 100:
        raise HarnessError("project trajectory requires a verified integer percent")
    start = iso(at)
    baseline = conn.execute(
        f"SELECT * FROM {BASELINE_TABLE} WHERE automation_id=?", (key,)
    ).fetchone()
    if baseline is None:
        end = iso(at + timedelta(hours=plan["horizon_hours"]))
        conn.execute(f"""INSERT INTO {BASELINE_TABLE}
            (automation_id,started_at,target_at,initial_percent,planning_horizon_hours,
             provisional,source) VALUES (?,?,?,?,?,?,?)""",
            (key, start, end, pct, plan["horizon_hours"], 1, plan["source"]))
    elif at < moment(baseline["started_at"]):
        raise HarnessError("cannot backdate a trajectory before its baseline")
    recent = conn.execute(f"""SELECT * FROM {EVENT_TABLE}
            WHERE automation_id=? AND kind='PROGRESS'
            ORDER BY observed_at DESC,id DESC LIMIT 1""", (key,)).fetchone()
    if recent is not None:
        previous = moment(recent["observed_at"])
        if at < previous:
            raise HarnessError("out-of-order progress observation rejected")
        if (pct == recent["actual_percent"] and
                at - previous < timedelta(minutes=30)):
            return False
    conn.execute(f"""INSERT OR IGNORE INTO {EVENT_TABLE}
        (automation_id,kind,observed_at,actual_percent,policy_digest)
        VALUES (?,?,?,?,?)""", (key, "PROGRESS", start, pct, _digest(monitor)))
    return True


def observe_all(db_path: Path, monitors: list[dict[str, Any]],
                at: datetime, config: Path = CONFIG) -> dict[str, int]:
    expected = plans(config)
    tracked = [m for m in monitors if m["kind"] == "project"]
    if {m["id"] for m in tracked} != set(expected):
        raise HarnessError("planned goals disagree with active project monitors")
    changes = {}
    with connect(db_path) as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        for m in tracked:
            changes[m["id"]] = int(observe(db, m, at, expected[m["id"]]))
    return changes


def _records(db_path: Path, key: str) -> tuple[dict | None, list[dict]]:
    with connect(db_path) as db:
        found = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if BASELINE_TABLE not in found or EVENT_TABLE not in found:
            return None, []
        base = db.execute(
            f"SELECT * FROM {BASELINE_TABLE} WHERE automation_id=?", (key,)
        ).fetchone()
        events = db.execute(f"""SELECT * FROM {EVENT_TABLE}
            WHERE automation_id=? ORDER BY observed_at,id""", (key,)).fetchall()
        return (dict(base) if base else None, [dict(x) for x in events])


def _timeline(
    base: dict, events: list[dict], pct: int, at: datetime
) -> dict[str, Any]:
    begin, original_end = moment(base["started_at"]), moment(base["target_at"])
    initial = int(base["initial_percent"])
    if at < begin:
        at = begin
    pivot = begin
    pivot_percent = initial
    target = original_end
    corrections: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for entry in events:
        timestamp = moment(entry["observed_at"])
        if entry["kind"] == "PROGRESS":
            history.append({"at": iso(timestamp), "percent": entry["actual_percent"]})
        elif entry["kind"] == "CORRECTION":
            expected_before = linear(pivot, target, pivot_percent, 100, timestamp)
            deviation_before = round(entry["actual_percent"] - expected_before, 2)
            if entry.get("old_plan_deviation_pp") is not None:
                if abs(entry["old_plan_deviation_pp"] - deviation_before) > 0.02:
                    raise HarnessError("trajectory correction proof has drifted")
            new_target = moment(entry["adjusted_target_at"])
            if new_target <= timestamp:
                raise HarnessError("adjusted end must be after correction")
            correction = {
                "at": iso(timestamp), "percent": entry["actual_percent"],
                "reason": entry["reason"], "evidence": entry["evidence"],
                "original_plan_gap_pp": round(
                    entry["actual_percent"] -
                    linear(begin, original_end, initial, 100, timestamp), 2),
                "pre_adjustment_gap_pp": deviation_before,
                "new_target_at": iso(new_target), "after_adjustment_gap_pp": None,
            }
            corrections.append(correction)
            pivot, pivot_percent, target = timestamp, entry["actual_percent"], new_target

    if not history:
        history = [{"at": iso(at), "percent": pct}]
    if at > moment(history[-1]["at"]):
        # Current accepted percentage is known at the requested report time.
        history.append({"at": iso(at), "percent": pct})
    elif at == moment(history[-1]["at"]):
        history[-1]["percent"] = pct

    baseline_expected = linear(begin, original_end, initial, 100, at)
    adjusted_expected = linear(pivot, target, pivot_percent, 100, at)
    baseline_gap = round(pct - baseline_expected, 2)
    plan_gap = round(pct - adjusted_expected, 2)

    for i, correction in enumerate(corrections):
        next_at = (moment(corrections[i + 1]["at"]) if i + 1 < len(corrections)
                   else at)
        samples = [p for p in history
                   if moment(correction["at"]) < moment(p["at"]) <= next_at]
        if samples:
            latest = samples[-1]
            expected = linear(moment(correction["at"]),
                              moment(correction["new_target_at"]),
                              correction["percent"], 100, moment(latest["at"]))
            correction["after_adjustment_gap_pp"] = round(latest["percent"] - expected, 2)

    remaining_seconds = max(0, (target - at).total_seconds())
    daily_needed = (100 - pct) / max(1 / 24, remaining_seconds / 86400)
    nominal_daily = (100 - initial) / max(1, (original_end - begin).total_seconds()/86400)
    return {
        "status": "TRACKED",
        "baseline_start": iso(begin),
        "baseline_target": iso(original_end),
        "baseline_percent": initial,
        "baseline_provisional": bool(base["provisional"]),
        "assumption": "7-day reference planning slope (not owner-approved deadline)",
        "current_percent": pct, "baseline_expected_percent": round(baseline_expected, 2),
        "adjusted_expected_percent": round(adjusted_expected, 2),
        "baseline_deviation_pp": baseline_gap,
        "adjusted_deviation_pp": plan_gap,
        "on_track": plan_gap >= -5,
        "adjusted_target": iso(target),
        "days_remaining_in_provisional_plan": round(remaining_seconds/86400, 2),
        "required_gain_per_day_pp": round(max(0, daily_needed), 2),
        "baseline_gain_per_day_pp": round(max(0, nominal_daily), 2),
        "correction_count": len(corrections),
        "corrections": corrections,
        "actual_observations": history,
    }


def view(db_path: Path, monitor: dict, at: datetime,
         config: Path = CONFIG) -> dict[str, Any] | None:
    if monitor["kind"] != "project":
        return None
    all_plans = plans(config)
    policy = all_plans.get(monitor["id"])
    if policy is None:
        raise HarnessError("active monitor has no trajectory plan")
    base, events = _records(db_path, monitor["id"])
    if base is None:
        # Read-only preview: do not fabricate historical samples.
        base = {
            "started_at": iso(at),
            "target_at": iso(at + timedelta(hours=policy["horizon_hours"])),
            "initial_percent": monitor["completion_percent"],
            "provisional": 1,
        }
        result = _timeline(base, [], monitor["completion_percent"], at)
        result["status"] = "PREVIEW_NO_PERSISTED_BASELINE"
        return result
    return _timeline(base, events, monitor["completion_percent"], at)


def record_correction(
    db_path: Path, monitor: dict, at: datetime, *,
    reason: str, evidence: str, next_horizon_hours: int | None = None,
) -> dict[str, Any]:
    """Append a direction decision. A correction never changes accepted gates."""
    if monitor["kind"] != "project":
        raise HarnessError("cannot replan ongoing maintenance as 100% complete")
    if not isinstance(reason, str) or len(reason.strip()) < 12:
        raise HarnessError("a concrete direction-change reason is required")
    if not isinstance(evidence, str) or not evidence.startswith(
        ("https://github.com/", "https://gitlab.com/")):
        raise HarnessError("direction change must cite its verifiable source")
    if next_horizon_hours is not None and (
        type(next_horizon_hours) is not int or not 24 <= next_horizon_hours <= 24 * 30
    ):
        raise HarnessError("new provisional horizon must be 1-30 days")
    base, events = _records(db_path, monitor["id"])
    if base is None:
        raise HarnessError("record a real accepted-gate baseline first")
    current = _timeline(base, events, monitor["completion_percent"], at)
    effective_target = moment(current["adjusted_target"])
    if next_horizon_hours is not None:
        new_target = at + timedelta(hours=next_horizon_hours)
    elif effective_target > at:
        new_target = effective_target
    else:
        raise HarnessError("expired reference plan requires explicit new horizon")
    old_gap = current["adjusted_deviation_pp"]
    with connect(db_path) as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute(f"""SELECT observed_at FROM {EVENT_TABLE}
            WHERE automation_id=? ORDER BY observed_at DESC,id DESC LIMIT 1""",
            (monitor["id"],)).fetchone()
        if latest and at < moment(latest["observed_at"]):
            raise HarnessError("cannot backdate a correction")
        db.execute(f"""INSERT INTO {EVENT_TABLE}
            (automation_id,kind,observed_at,actual_percent,policy_digest,
             reason,evidence,adjusted_target_at,old_plan_deviation_pp)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (monitor["id"], "CORRECTION", iso(at), monitor["completion_percent"],
             _digest(monitor), reason.strip(), evidence, iso(new_target), old_gap))
    return {
        "at": iso(at),
        "deviation_before_pp": old_gap,
        "new_planning_target": iso(new_target),
        "new_percent": monitor["completion_percent"],
        "baseline_preserved": True,
    }


def render_svg(data: dict[str, Any], *, width: int = 760, height: int = 300) -> str:
    """Static accessible SVG; no JS, embeds no secrets or untrusted HTML."""
    start = moment(data["baseline_start"])
    end = max(moment(data["baseline_target"]), moment(data["adjusted_target"]),
              moment(data["actual_observations"][-1]["at"]))
    seconds = max(1, (end - start).total_seconds())
    left, right, top, bottom = 65, width - 22, 18, height - 43
    plot_w, plot_h = right - left, bottom - top

    def x(dt: datetime) -> float:
        return left + plot_w * bounded((dt-start).total_seconds()/seconds*100)/100

    def y(pct: float) -> float:
        return bottom - plot_h * bounded(pct)/100

    def line(points: list[tuple[datetime, float]], color: str,
             dashed: bool = False, cls: str = "") -> str:
        if not points:
            return ""
        coords = " ".join(f"{x(t):.1f},{y(p):.1f}" for t, p in points)
        dash = ' stroke-dasharray="8 5"' if dashed else ""
        return (f'<polyline class="{cls}" points="{coords}" fill="none" '
                f'stroke="{color}" stroke-width="3" stroke-linecap="round"'
                f' stroke-linejoin="round"{dash}/>')
    lines = [
        '<svg viewBox="0 0 %d %d" width="100%%" role="img" '
        'aria-label="Đường mục tiêu, tiến độ thực tế và các mốc chỉnh hướng" '
        'xmlns="http://www.w3.org/2000/svg">' % (width, height),
    ]
    for p in (0, 25, 50, 75, 100):
        lines.append(f'<line x1="{left}" y1="{y(p):.1f}" x2="{right}" '
                     f'y2="{y(p):.1f}" stroke="#415064" stroke-width="1"/>')
        lines.append(f'<text x="55" y="{y(p)+4:.1f}" font-size="12" '
                     f'text-anchor="end" fill="#aac0d0">{p}%</text>')
    for fraction, label in ((0, "Bắt đầu"),(.5,"Giữa kế hoạch"),(1,"Mốc tham chiếu")):
        cx = left + plot_w*fraction
        lines.append(f'<text x="{cx:.1f}" y="{height-16}" font-size="11" '
                     f'text-anchor="{"start" if fraction == 0 else "end" if fraction == 1 else "middle"}" '
                     f'fill="#aac0d0">{label}</text>')

    lines.append(line([
        (start, data["baseline_percent"]),
        (moment(data["baseline_target"]),100)], "#8fc1ff", dashed=True,
        cls="trajectory-original-baseline"))

    corrections = data["corrections"]
    if corrections:
        final = corrections[-1]
        lines.append(line([(moment(final["at"]),final["percent"]),
                           (moment(final["new_target_at"]),100)],
                          "#ffcd88", dashed=True, cls="trajectory-revised-plan"))
    actual = [(moment(q["at"]), q["percent"]) for q in data["actual_observations"]]
    step = []
    for t,p in actual:
        if step and t > step[-1][0]:
            step.append((t,step[-1][1]))
        step.append((t,p))
    lines.append(line(step, "#69d2a4", cls="trajectory-observed"))
    for t,p in actual[-1:]:
        lines.append(f'<circle cx="{x(t):.1f}" cy="{y(p):.1f}" r="5" fill="#69d2a4"/>')
    for i, c in enumerate(corrections):
        cx = x(moment(c["at"]))
        lines.append(f'<line x1="{cx:.1f}" y1="{top}" x2="{cx:.1f}" '
                     f'y2="{bottom}" stroke="#ffcd88" stroke-dasharray="4 4" '
                     f'opacity=".65"/>')
        lines.append(f'<text x="{cx+4:.1f}" y="{top+14+i%3*13}" font-size="11" '
                     f'fill="#ffcd88">↪{i+1}</text>')
    lines.append('</svg>')
    return "".join(lines)


def markdown_line(data: dict[str, Any]) -> str:
    drift = data["adjusted_deviation_pp"]
    label = "đúng/nhanh hơn" if drift >= 0 else "chậm hơn"
    return (f"Đường thực tế {data['current_percent']}% · kế hoạch tham chiếu "
            f"{data['adjusted_expected_percent']:.1f}% · lệch {drift:+.1f} điểm % "
            f"({label}); chỉnh hướng {data['correction_count']} lần · cần "
            f"{data['required_gain_per_day_pp']:.1f} điểm %/ngày tới mốc giả định.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("view", "adjust"))
    p.add_argument("--db", default=str(STATE_DB))
    p.add_argument("--automation-id", required=True)
    p.add_argument("--reason")
    p.add_argument("--evidence")
    p.add_argument("--horizon-hours", type=int)
    args = p.parse_args()
    from scripts.harness_progress import summarize
    db = Path(args.db).expanduser().resolve()
    snapshot = summarize(db)
    m = next((x for x in snapshot["monitors"] if x["id"] == args.automation_id), None)
    if m is None:
        raise SystemExit("Unknown or paused monitor")
    at = moment(snapshot["observed_at"])
    if args.action == "adjust":
        if not args.reason or not args.evidence:
            p.error("adjust requires --reason and --evidence")
        out = record_correction(db, m, at, reason=args.reason,
                                evidence=args.evidence,
                                next_horizon_hours=args.horizon_hours)
    else:
        out = view(db, m, at)
    print("HARNESS_TRAJECTORY=" + json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
