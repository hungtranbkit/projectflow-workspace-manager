"""Evidence-weighted project progress + immutable child TTL dashboard.

A completed technical canary does NOT imply project completion. Project
percent counts only independently reviewed accepted gate weights from the
versioned policy file. A child TTL bar shows time consumed, not code progress.
No network, GitHub writes, task dispatch or process deletion occurs here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import tempfile
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.agent_harness_mvp import HarnessStore, HarnessError
from scripts.harness_monitor_bridge import (
    STATE_DB, REGISTRY, load_registry, connect, require_binding,
)
from scripts.harness_managed_exec import atomic_json

HERE = Path(__file__).resolve().parents[1]
DEFAULT_GATES = HERE / "config/harness_progress_gates.json"
ICT = timezone(timedelta(hours=7), "ICT")  # Vietnam UTC+07, no tzdata dependency
FINAL_STATES = {"DONE_VERIFIED", "FAILED", "EXPIRED"}
GATE_STATES = {"PASSED", "IN_PROGRESS", "BLOCKED", "NOT_STARTED"}


def aware(value: str) -> datetime:
    obj = datetime.fromisoformat(value)
    if obj.tzinfo is None or obj.utcoffset() is None:
        raise HarnessError("timestamps must be timezone-aware")
    return obj.astimezone(timezone.utc)


def duration(seconds: float) -> str:
    secs = max(0, int(seconds))
    h, m = divmod(secs // 60, 60)
    if h >= 1:
        return f"{h}h {m:02d}m"
    return f"{m}m {secs % 60:02d}s"


def bar(percent: int, cells: int = 12) -> str:
    count = max(0, min(cells, round(percent * cells / 100)))
    return "█" * count + "░" * (cells - count)


def read_gates(path: Path = DEFAULT_GATES) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("goals"), list):
        raise HarnessError("unreviewed progress policy")
    out: dict[str, dict[str, Any]] = {}
    for g in payload["goals"]:
        ident = g["automation_id"]
        if ident in out:
            raise HarnessError("duplicate progress goal")
        kind = g.get("kind")
        gates = g.get("gates")
        if kind not in {"project", "maintenance"} or not isinstance(gates, list):
            raise HarnessError("invalid goal type")
        if kind == "project" and (not gates or
                sum(t["weight"] for t in gates) != 100):
            raise HarnessError("accepted gate weights must sum to 100")
        if kind == "maintenance" and gates:
            raise HarnessError("maintenance is continuous and has no completion percentage")
        names = set()
        for gate in gates:
            key = gate["id"]
            if key in names or gate.get("status") not in GATE_STATES:
                raise HarnessError("duplicate gate or unknown status")
            names.add(key)
            if type(gate.get("weight")) is not int or not 0 < gate["weight"] <= 100:
                raise HarnessError("invalid gate weight")
            if gate["status"] == "PASSED":
                if not gate.get("proof") or not (gate.get("accepted_at") or gate.get("accepted_on")):
                    raise HarnessError("PASS requires acceptance proof + date")
                if gate.get("accepted_at"):
                    aware(gate["accepted_at"])
                else:
                    from datetime import date
                    date.fromisoformat(gate["accepted_on"])
        out[ident] = g
    return out


def task_clock(task: Any, reference: datetime) -> dict[str, Any]:
    total = (task.expires_at - task.created_at).total_seconds()
    if not 0 < total <= 86400:
        raise HarnessError("task deadline exceeds allowed 24h or is malformed")
    ended = task.state in FINAL_STATES
    event_time = task.verified_at if ended and task.verified_at else reference
    elapsed_raw = max(0.0, (event_time - task.created_at).total_seconds())
    consumed = min(total, elapsed_raw)
    time_pct = min(100, round(consumed / total * 100))
    remaining = max(0.0, total - elapsed_raw) if not ended else None
    return {
        "id": task.id,
        "name": task.child_goal,
        "state": task.state,
        "cleanup": task.cleanup_state,
        "created_at": task.created_at.isoformat(),
        "expires_at": task.expires_at.isoformat(),
        "deadline_local": task.expires_at.astimezone(ICT).strftime("%d/%m %H:%M"),
        "ttl_seconds": round(total),
        "budget_elapsed_seconds": round(consumed),
        "time_used_percent": time_pct,
        "time_used_bar": bar(time_pct),
        "time_used_label": duration(consumed),
        "time_budget_label": duration(total),
        "remaining_seconds": round(remaining) if remaining is not None else None,
        "remaining_label": duration(remaining) if remaining is not None else "đã kết thúc",
        "overdue": not ended and reference >= task.expires_at,
        "finished": ended,
        "project_DoD": ("NOT_TESTED_BY_CANARY"
                         if task.child_goal.startswith("CANARY ONLY:")
                         else "INDEPENDENT_PROJECT_ACCEPTANCE_REQUIRED"),
    }


def summarize(
    db_path: Path,
    *,
    registry_path: Path = REGISTRY,
    gate_path: Path = DEFAULT_GATES,
    reference: datetime | None = None,
    recent_limit: int = 3,
) -> dict[str, Any]:
    at = reference or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise HarnessError("reference time must be aware")
    at = at.astimezone(timezone.utc)
    if not db_path.is_file():
        raise HarnessError("Harness SQLite DB absent")
    registry = load_registry(registry_path)
    policies = read_gates(gate_path)
    if set(policies) != {x for x, v in registry.items() if v["configured_enabled"]}:
        raise HarnessError("progress plans do not match currently enabled monitor registry")
    store = HarnessStore(db_path)
    with connect(db_path) as db:
        tables = {x[0] for x in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        activation = {}
        if "harness_monitor_activation_events" in tables:
            activation = {x["automation_id"]: x["activated_at"]
                          for x in db.execute(
                              "SELECT automation_id,activated_at FROM harness_monitor_activation_events")}
        monitors = []
        for ident, entry in registry.items():
            if not entry["configured_enabled"]:
                continue
            policy = policies[ident]
            _, binding = require_binding(ident, registry, db)
            gates = policy["gates"]
            passed = sum(x["weight"] for x in gates if x["status"] == "PASSED")
            if policy["kind"] == "project" and passed == 100 and any(
                x["status"] != "PASSED" for x in gates
            ):
                raise HarnessError("inconsistent project completion")
            tasks = store.list_tasks(main_goal=binding["main_goal"])
            open_tasks = [x for x in tasks
                          if x.state not in FINAL_STATES or x.cleanup_state == "CLEANUP_PENDING"]
            # The project has no 24h SLA. Only its individual child tasks do.
            initiated = activation.get(ident, binding["installed_at"])
            since = aware(initiated)
            monitors.append({
                "id": ident, "name": policy["display_name"],
                "kind": policy["kind"], "mode": binding["integration_mode"],
                "goal": binding["main_goal"],
                "completion_percent": passed if policy["kind"] == "project" else None,
                "completion_bar": bar(passed) if policy["kind"] == "project" else None,
                "progress_metric": "evidence-weighted accepted gates, not code volume",
                "reviewed_on": json.loads(gate_path.read_text(encoding="utf-8"))["reviewed_on"],
                "gates_passed": sum(x["status"] == "PASSED" for x in gates),
                "gates_total": len(gates),
                "gates": gates,
                "goal_elapsed_seconds": max(0, round((at - since).total_seconds())),
                "goal_elapsed_label": duration((at - since).total_seconds()),
                "goal_has_deadline": False,
                "wip": len(open_tasks),
                "max_wip": entry["max_children"],
                "child_passed_technical": sum(x.state == "DONE_VERIFIED" and
                                              x.cleanup_state == "CLEANED_VERIFIED" for x in tasks),
                "child_failed": sum(x.state in {"FAILED", "EXPIRED"} for x in tasks),
                "active_tasks": [task_clock(t, at) for t in open_tasks],
                "recent_tasks": [task_clock(t, at) for t in tasks[-recent_limit:]],
            })
    health_file = db_path.parent / "monitor-health.json"
    health: dict[str, Any] = {"status": "UNKNOWN", "reason": "health readback absent"}
    if health_file.is_file():
        try:
            value = json.loads(health_file.read_text(encoding="utf-8"))
            age = (at - aware(value["observed_at"])).total_seconds()
            health = {
                "status": "HEALTHY" if value.get("healthy") is True and
                            0 <= age <= 900 else "FAILED_OR_STALE",
                "age_seconds": max(0, int(age)),
                "scheduler_healthy": value.get("scheduler_healthy") is True,
                "bridge_healthy": value.get("bridge_healthy") is True,
                "observed_at": value["observed_at"],
            }
        except (OSError, ValueError, KeyError, TypeError):
            health = {"status": "UNKNOWN", "reason": "malformed health evidence"}
    from scripts.harness_trajectory import view as trajectory_view
    for monitor in monitors:
        monitor["trajectory"] = trajectory_view(db_path, monitor, at)
    return {
        "schema": "harness-progress-v1",
        "observed_at": at.isoformat(),
        "observed_local": at.astimezone(ICT).strftime("%d/%m/%Y %H:%M ICT"),
        "metric_explanation": (
            "A project percent is the sum of PASSED evidence gates only. "
            "24h applies to each child, not to the entire project. "
            "A canary DONE does not increase business progress."),
        "monitor_count": len(monitors),
        "paused_count": len(registry) - len(monitors),
        "health": health,
        "monitors": monitors,
    }


def markdown(snapshot: dict[str, Any]) -> str:
    lines = [f"Harness • {snapshot['observed_local']}",
             f"Service: {snapshot['health']['status']} • "
             f"{snapshot['monitor_count']} đang bật, {snapshot['paused_count']} tạm dừng."]
    for m in snapshot["monitors"]:
        if m["completion_percent"] is None:
            lines.append(f"🔧 {m['name']} • bảo trì liên tục, không có % hoàn tất.")
        else:
            lines.append(f"🎯 {m['name']} • {m['completion_bar']} "
                         f"{m['completion_percent']}% gate đã nghiệm thu "
                         f"({m['gates_passed']}/{m['gates_total']})")
        lines.append(f"   WIP {m['wip']}/{m['max_wip']} • "
                     f"đã theo dõi {m['goal_elapsed_label']}; "
                     "dự án không có deadline 24h.")
        if m.get("trajectory"):
            from scripts.harness_trajectory import markdown_line
            lines.append("   ↗ " + markdown_line(m["trajectory"]))
            lines.append("   🗓 Đường chéo 7 ngày là mốc THAM CHIẾU, chưa phải hạn cam kết.")
        for t in m["active_tasks"]:
            lines.append(
                f"   ⏱ #{t['id']} {t['time_used_bar']} "
                f"{t['time_used_percent']}% hạn mức, "
                f"đã dùng {t['time_used_label']}/{t['time_budget_label']}; "
                f"còn {t['remaining_label']} • {t['state']}"
                + (" • QUÁ HẠN" if t["overdue"] else ""))
    lines.append("Lưu ý: % chỉ tính gate có bằng chứng PASS; không phải % code.")
    return "\n".join(lines)


def html(snapshot: dict[str, Any]) -> str:
    parts = ['<!doctype html><html lang="vi"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="refresh" content="300">',
             '<title>Harness · Tiến độ điều phối</title>',
             '<style>body{font:15px system-ui,sans-serif;background:#101b26;color:#e8f0fa;margin:0}'
             'main{max-width:760px;margin:auto;padding:20px 16px}h1{font-size:23px;margin:8px 0 4px}'
             '.meta{color:#9db1c5;font-size:13px}.card{border:1px solid #314355;border-radius:16px;'
             'padding:17px;margin:14px 0;background:#182736}.row{display:flex;justify-content:'
             'space-between;gap:12px;align-items:center}.label{font-weight:650;font-size:17px}'
             '.value{font-weight:800;font-size:23px}.track{height:12px;border-radius:8px;'
             'background:#344354;overflow:hidden;margin:9px 0}.fill{background:#67c69c;'
             'height:100%;border-radius:8px}.time{background:#f2b66c}.small{color:#bacbdc;'
             'font-size:13px;margin:7px 0}.task{border-top:1px solid #344354;padding-top:12px;'
             'margin-top:12px}.bad{color:#f7a2a2}.gate{margin:6px 0;font-size:13px}'
             '.tag{display:inline-block;border-radius:8px;padding:3px 7px;background:#344354}'
             '</style></head><body><main>',
             '<h1>Tiến độ Harness</h1>',
             f'<p class="meta">Cập nhật {escape(snapshot["observed_local"])} · '
             f'{snapshot["monitor_count"]} bật / {snapshot["paused_count"]} tạm dừng · '
             f'Health: {escape(snapshot["health"]["status"])}</p>']
    for m in snapshot["monitors"]:
        p = m["completion_percent"]
        val = "Bảo trì" if p is None else f"{p}%"
        parts += ['<section class="card"><div class="row">',
                  f'<span class="label">{escape(m["name"])}</span>',
                  f'<strong class="value">{escape(val)}</strong></div>']
        if p is not None:
            parts += [f'<div class="track" role="progressbar" aria-label="Gate đã nghiệm thu" '
                      f'aria-valuenow="{p}" aria-valuemin="0" aria-valuemax="100">'
                      f'<div class="fill" style="width:{p}%"></div></div>',
                      f'<div class="small">Đã PASS {m["gates_passed"]}/{m["gates_total"]} gate · '
                      'Không tính PR nháp hay canary như nghiệm thu dự án</div>']
        parts += [f'<div class="small">WIP {m["wip"]}/{m["max_wip"]} · '
                  f'Đã theo dõi {escape(m["goal_elapsed_label"])} · '
                  '24 giờ chỉ áp dụng mỗi task con</div>']
        for g in m["gates"]:
            symbol = "✓" if g["status"] == "PASSED" else "○"
            parts.append(f'<div class="gate">{symbol} {escape(g["id"])} '
                         f'{escape(g["name"])} <span class="tag">{g["weight"]}% '
                         f'{escape(g["status"])}</span></div>')
        for t in m["active_tasks"]:
            parts += ['<div class="task">',
                      f'<div class="row"><strong>Task #{t["id"]}</strong>'
                      f'<span>{escape(t["state"])}</span></div>',
                      f'<div class="track" role="progressbar" aria-label="Hạn mức thời gian" '
                      f'aria-valuenow="{t["time_used_percent"]}" aria-valuemin="0" '
                      f'aria-valuemax="100"><div class="fill time" '
                      f'style="width:{t["time_used_percent"]}%"></div></div>',
                      f'<div class="small">Đã dùng {escape(t["time_used_label"])} '
                      f'/ {escape(t["time_budget_label"])} · '
                      f'còn {escape(t["remaining_label"])} · '
                      f'hạn {escape(t["deadline_local"])}</div></div>']
        if not m["active_tasks"]:
            parts.append('<p class="small">Không có task nào đang chiếm WIP.</p>')
        tr = m.get("trajectory")
        if tr is not None:
            from scripts.harness_trajectory import render_svg
            drift = tr["adjusted_deviation_pp"]
            gap = "nhanh hơn/đúng kế hoạch" if drift >= 0 else "chậm hơn"
            parts += [
                '<div class="task"><div class="small"><strong>Quỹ đạo đến mục tiêu</strong>'
                ' · đường kế hoạch và tiến độ theo thời gian</div>',
                render_svg(tr),
                '<div class="small">🔵 kế hoạch gốc (đường xiên) · '
                '🟢 thực tế nghiệm thu · 🟠 kế hoạch sau chỉnh hướng</div>',
                f'<div class="small">Kế hoạch tham chiếu: '
                f'{tr["adjusted_expected_percent"]:.1f}% · thực tế '
                f'{tr["current_percent"]}% · <strong>{drift:+.1f} điểm % '
                f'({escape(gap)})</strong></div>',
                f'<div class="small">Đã chỉnh hướng {tr["correction_count"]} lần · '
                f'cần {tr["required_gain_per_day_pp"]:.2f} điểm %/ngày '
                'theo mốc giả định</div>',
                '<div class="small">Mốc tham chiếu 7 ngày là giả định '
                'để quan sát độ lệch, KHÔNG phải deadline được duyệt.</div>',
            ]
            if tr["corrections"]:
                parts.append('<div class="small"><strong>Lịch sử chỉnh hướng:</strong></div>')
                for c in tr["corrections"][-5:]:
                    after = ("chưa có quan sát sau chỉnh" if
                             c["after_adjustment_gap_pp"] is None else
                             f'{c["after_adjustment_gap_pp"]:+.1f} điểm %')
                    parts.append(
                        f'<div class="small">↪ {escape(c["at"][:16])} · '
                        f'khi chỉnh lệch {c["pre_adjustment_gap_pp"]:+.1f} điểm % · '
                        f'sau chỉnh lệch {escape(after)} · '
                        f'{escape(c["reason"] or "")}</div>'
                    )
            parts.append('</div>')
        finished = [t for t in m["recent_tasks"] if t["finished"]]
        if finished:
            parts.append('<div class="small"><strong>Task gần nhất — đã kết thúc</strong></div>')
        for t in finished[-3:][::-1]:
            parts += ['<div class="task">',
                      f'<div class="row"><strong>#{t["id"]} '
                      f'{escape(t["state"])}</strong>'
                      f'<span>{escape(t["cleanup"])}</span></div>',
                      f'<div class="track" role="progressbar" aria-label="Hạn mức task kết thúc" '
                      f'aria-valuenow="{t["time_used_percent"]}" aria-valuemin="0" '
                      f'aria-valuemax="100"><div class="fill time" '
                      f'style="width:{t["time_used_percent"]}%"></div></div>',
                      f'<div class="small">Đã dùng {escape(t["time_used_label"])} '
                      f'/ {escape(t["time_budget_label"])} · đã kết thúc; '
                      f'không còn đồng hồ đếm ngược</div></div>']
        parts.append('</section>')
    parts += ['<p class="small">Thanh xanh = % gate đã được xác nhận bằng chứng. '
              'Thanh cam = % hạn mức thời gian task đã tiêu thụ. '
              'Không có hạn 24h cho cả dự án.</p></main></body></html>']
    return "".join(parts)


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.stem + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as writer:
            writer.write(content)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_private_dashboard(snapshot: dict[str, Any], directory: Path) -> dict[str, str]:
    if not directory.is_dir() or directory.is_symlink():
        raise HarnessError("state directory not available or symlink")
    json_path = directory / "monitor-progress.json"
    html_path = directory / "monitor-progress.html"
    atomic_json(json_path, snapshot)
    _write_private(html_path, html(snapshot))
    return {"json": str(json_path), "html": str(html_path)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(STATE_DB))
    p.add_argument("--format", choices=("json", "markdown", "html"), default="markdown")
    p.add_argument("--write", action="store_true",
                   help="write private JSON and responsive HTML to existing state directory")
    p.add_argument("--gate-policy", default=str(DEFAULT_GATES))
    args = p.parse_args()
    db_path = Path(args.db).expanduser().resolve()
    snap = summarize(db_path, gate_path=Path(args.gate_policy))
    if args.write:
        from scripts.harness_trajectory import observe_all, moment
        at = moment(snap["observed_at"])
        observe_all(db_path, snap["monitors"], at)
        # Include the just-persisted checkpoint in both JSON and the SVG.
        snap = summarize(db_path, gate_path=Path(args.gate_policy), reference=at)
        files = write_private_dashboard(snap, db_path.parent)
        snap["saved_files"] = files
    if args.format == "json":
        print("HARNESS_PROGRESS=" + json.dumps(snap, ensure_ascii=False))
    elif args.format == "html":
        print(html(snap))
    else:
        print(markdown(snap))


if __name__ == "__main__":
    main()
