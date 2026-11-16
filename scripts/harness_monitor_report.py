"""Short read-only, fail-closed evidence for ChatGPT scheduled monitors.

One stable, single-line report from the existing private 5-minute
monitor-progress.json checkpoint. Designed for a single Terminal MCP
send_wait call: no SSH helper, new agent, source checkout or stdout flood.
Never promotes technical canaries, PRs, or CI to business acceptance.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

DEFAULT = Path.home() / ".local/state/projectflow-harness/monitor-progress.json"


def aware(value: str) -> datetime:
    d = datetime.fromisoformat(value)
    if d.tzinfo is None or d.utcoffset() is None:
        raise ValueError("timestamp missing timezone")
    return d.astimezone(timezone.utc)


def percent_bar(p: int | None, cells: int = 12) -> str | None:
    if p is None:
        return None
    if type(p) is not int or not 0 <= p <= 100:
        raise ValueError("progress percent is not verified")
    n = round(p * cells / 100)
    return "█" * n + "░" * (cells - n)


def report(snapshot: dict, ident: str, now: datetime,
           *, max_age_seconds: int = 900) -> dict:
    """Do not return stale percentages masquerading as a fresh readback."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("observation time must be aware")
    observed = aware(snapshot["observed_at"])
    age = (now.astimezone(timezone.utc) - observed).total_seconds()
    if age < -30 or age > max_age_seconds or snapshot.get("health", {}).get("status") != "HEALTHY":
        return {"status": "HARNESS_UNKNOWN", "automation_id": ident,
                "reason": "STALE_OR_UNHEALTHY", "age_seconds": round(age)}
    matches = [m for m in snapshot.get("monitors", [])
               if m.get("id") == ident]
    if len(matches) != 1:
        return {"status": "HARNESS_UNKNOWN", "automation_id": ident,
                "reason": "MONITOR_NOT_FOUND_OR_PAUSED"}
    m = matches[0]
    pct = m.get("completion_percent")
    if pct is not None and (type(pct) is not int or not 0 <= pct <= 100):
        raise ValueError("invalid gate percent")
    tr = m.get("trajectory")
    chart = None
    if pct is not None and tr:
        initial = tr.get("baseline_percent")
        if type(initial) is not int or not 0 <= initial <= 100:
            raise ValueError("invalid baseline")
        original = [
            {"at": tr["baseline_start"], "percent": initial},
            {"at": tr["baseline_target"], "percent": 100}]
        observations = tr.get("actual_observations", [])
        if not isinstance(observations, list) or not observations:
            raise ValueError("no independent observations")
        measured = observations[:1] + observations[-11:] if len(observations) > 12 else observations
        revised = None
        if tr.get("corrections"):
            pivot = tr["corrections"][-1]
            revised = [{"at": pivot["at"], "percent": pivot["percent"]},
                       {"at": pivot["new_target_at"], "percent": 100}]
        chart = {"original_plan": original, "actual": measured,
                 "revised_plan": revised,
                 "pivot_count": tr.get("correction_count", 0),
                 "baseline_deviation_pp": tr.get("baseline_deviation_pp"),
                 "current_deviation_pp": tr.get("adjusted_deviation_pp"),
                 "current_expected_percent": tr.get("adjusted_expected_percent"),
                 "planning_horizon_provisional": tr.get("baseline_provisional", True),
                 "corrections": tr.get("corrections", [])[-3:]}
    children = []
    for t in m.get("active_tasks", []):
        created, deadline = aware(t["created_at"]), aware(t["expires_at"])
        max_secs = (deadline - created).total_seconds()
        if not 0 < max_secs <= 86400:
            raise ValueError("task exceeds immutable 24h bound")
        elapsed = max(0, (now.astimezone(timezone.utc) - created).total_seconds())
        remaining = max(0, (deadline - now.astimezone(timezone.utc)).total_seconds())
        n = min(100, round(elapsed / max_secs * 100))
        children.append({"id": t["id"], "state": t["state"], "cleanup": t["cleanup"],
                         "time_used_percent": n, "time_used_bar": percent_bar(n),
                         "elapsed_seconds": min(round(elapsed), round(max_secs)),
                         "ttl_seconds": round(max_secs),
                         "remaining_seconds": round(remaining),
                         "deadline_ict": t.get("deadline_local"),
                         "overdue": remaining == 0})
    return {
        "status": "VERIFIED_READBACK", "automation_id": ident,
        "name": m["name"], "as_of": snapshot["observed_at"],
        "age_seconds": round(age),
        "completion_percent": pct, "completion_bar": percent_bar(pct),
        "gates_passed": m.get("gates_passed"), "gates_total": m.get("gates_total"),
        "wip": m.get("wip"), "max_wip": m.get("max_wip"),
        "task_time_budget": children,
        "project_has_24h_deadline": False,
        "chart": chart,
        "project_business_accepted": pct == 100 if pct is not None else None,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Compact per-monitor verified snapshot")
    p.add_argument("--automation-id", required=True)
    p.add_argument("--snapshot", default=str(DEFAULT))
    args = p.parse_args()
    try:
        obj = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        result = report(obj, args.automation_id, datetime.now(timezone.utc))
    except (KeyError, ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
        result = {"status": "HARNESS_UNKNOWN", "automation_id": args.automation_id,
                  "reason": f"{type(exc).__name__}:{str(exc)[:90]}"}
    # One short, machine-readable line. No sensitive goal text or customer data.
    print("HARNESS_REPORT=" + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if result["status"] != "VERIFIED_READBACK":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
