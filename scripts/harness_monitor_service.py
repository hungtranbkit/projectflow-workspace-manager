"""Single fail-closed systemd entrypoint for Harness + monitor reconciliation.

Do NOT use ExecStartPost=- for the bridge: systemd reports success despite
bridge failures when the "-" prefix masks exit status. This bounded wrapper
owns both passes, records independent health fields and exits nonzero when
either part fails. It never sends customer messages or deletes sessions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.harness_managed_exec import atomic_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path.home() / ".local/state/projectflow-harness/harness-mvp.db"
DEFAULT_TMCP_PYTHON = Path("/home/dell/workspace/terminal-mcp/.venv/bin/python")
DEFAULT_PF_PYTHON = Path("/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python")


def _parse(output: str, prefix: str) -> dict[str, Any]:
    lines = [line for line in output.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        raise ValueError(f"expected one {prefix} receipt, got {len(lines)}")
    value = json.loads(lines[0][len(prefix):])
    if not isinstance(value, dict):
        raise ValueError("monitor receipt is not an object")
    return value


def _execute(command: list[str], run: Callable[..., Any], timeout: int) -> Any:
    return run(command, capture_output=True, text=True, timeout=timeout)


def reconcile(
    *, db: Path, health_file: Path,
    python_core: Path = DEFAULT_PF_PYTHON,
    python_bridge: Path = DEFAULT_TMCP_PYTHON,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[int, dict[str, Any]]:
    if (not db.is_file() or not python_core.is_file() or
            not python_bridge.is_file() or health_file.parent != db.parent):
        raise ValueError("missing trusted DB/runtime or health output outside state dir")
    record: dict[str, Any] = {
        "schema": "harness-monitor-health-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scheduler_healthy": False,
        "bridge_healthy": False,
        "agent_verified": False,
        "business_accepted": False,  # Never infer project acceptance from a scheduler tick.
        "errors": [],
    }
    stages = [
        ("scheduler", python_core, ROOT / "scripts/harness_reconcile.py", "--apply",
         "HARNESS_RECONCILE="),
        ("bridge", python_bridge, ROOT / "scripts/harness_monitor_bridge.py", "reconcile",
         "HARNESS_MONITOR="),
    ]
    for stage, python, script, action, prefix in stages:
        try:
            result = _execute(
                [str(python), str(script), action, "--db", str(db)],
                runner, 23)
            if result.returncode:
                raise RuntimeError(f"process exit {result.returncode}")
            value = _parse(result.stdout, prefix)
            if stage == "bridge":
                observations = value.get("results")
                if not isinstance(observations, list):
                    raise ValueError("bridge response lacks results")
                errors = sum(1 for entry in observations
                             if not isinstance(entry, dict) or "error" in entry)
                record["bridge_checked"] = value.get("checked", 0)
                record["bridge_observation_errors"] = errors
                if errors:
                    raise RuntimeError(f"bridge readback errors {errors}")
            else:
                # The core reconcile may have task-level errors while Python exits 0.
                managed = value.get("managed", [])
                if not isinstance(managed, list):
                    raise ValueError("invalid managed receipts")
                problems = sum(1 for entry in managed
                               if isinstance(entry, dict) and entry.get("error"))
                record["managed_errors"] = problems
                if problems:
                    raise RuntimeError(f"managed reconciliation errors {problems}")
            record[f"{stage}_healthy"] = True
        except (subprocess.TimeoutExpired, OSError, ValueError, RuntimeError,
                json.JSONDecodeError) as exc:
            record["errors"].append(f"{stage}:{type(exc).__name__}:{str(exc)[:100]}")
    # Explicitly separate technical scheduler health from agent lifecycle.
    # A completed cron tick cannot be claimed as a completed child or project.
    from app.services.agent_harness_mvp import HarnessStore
    try:
        tasks = HarnessStore(db).list_tasks()
        record["agent_verified_count"] = sum(
            t.state in {"RUNNING_VERIFIED", "DONE_VERIFIED"} for t in tasks)
        record["agent_verified"] = record["agent_verified_count"] > 0
        record["stalled_children"] = [
            t.id for t in tasks if t.state in {"STALLED", "BLOCKED", "UNKNOWN"}]
        record["cleanup_pending"] = [
            t.id for t in tasks if t.cleanup_state == "CLEANUP_PENDING"]
        record["dispatch_unverified"] = [
            t.id for t in tasks
            if t.state in {"NOT_DISPATCHED", "DISPATCHED"}
            and (datetime.now(timezone.utc) - t.created_at).total_seconds() >= 600
        ]
        record["task_count"] = len(tasks)
    except (OSError, ValueError, RuntimeError) as exc:
        record["errors"].append(f"snapshot:{type(exc).__name__}:{str(exc)[:80]}")
    record["healthy"] = (record["scheduler_healthy"] and record["bridge_healthy"]
                         and not record["errors"])
    record["attention_required"] = bool(
        record.get("stalled_children") or record.get("cleanup_pending")
        or record.get("dispatch_unverified"))
    # Always persist the real health before reporting success or failure.
    atomic_json(health_file, record)
    return (0 if record["healthy"] else 1), record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--health-file")
    args = parser.parse_args()
    db = Path(args.db).expanduser().resolve()
    health = (Path(args.health_file).expanduser().resolve()
              if args.health_file else db.parent / "monitor-health.json")
    try:
        code, receipt = reconcile(db=db, health_file=health)
    except (OSError, ValueError, RuntimeError) as exc:
        print("HARNESS_MONITOR_HEALTH=" + json.dumps({
            "healthy": False, "error": f"{type(exc).__name__}:{str(exc)[:120]}"}))
        raise SystemExit(1) from exc
    print("HARNESS_MONITOR_HEALTH=" + json.dumps(receipt, sort_keys=True))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
