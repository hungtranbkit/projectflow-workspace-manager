"""One-shot, safe technical canary for an enabled Harness monitor parent.

This is a smoke probe of the MONITOR-TO-HARNESS checkpoint, NOT a proof that a
commercial SKU is licensed, that the iPhone/NEC integration works, or that
the monitored project is complete.

Explicit --launch required. Never launches a paused or maintenance monitor.
Re-running reuses the existing task, never silently duplicates it.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.agent_harness_mvp import HarnessStore, HarnessError
from scripts.harness_managed_exec import ManagedHarness
from scripts.harness_monitor_bridge import (
    STATE_DB, load_registry, connect, require_binding,
)

CHILD_GOALS = {
    "6af65f5c6cdc8191a2ecc2f47085bea6":
        "CANARY ONLY: verify CDTM monitor can record a source-rights gate checkpoint",
    "6af698c9a5308191899c6a7e013cee5f":
        "CANARY ONLY: verify Screen Remote monitor can record NEC/iPhone runtime gate checkpoint",
}
MARKERS = {
    "6af65f5c6cdc8191a2ecc2f47085bea6": "HARNESS_CDTM_MONITOR_CANARY_V1",
    "6af698c9a5308191899c6a7e013cee5f": "HARNESS_SCREEN_MONITOR_CANARY_V1",
}


def run_one(
    ident: str, db_path: Path, repo_path: Path, launch: bool = False,
    manager_cls: type[ManagedHarness] = ManagedHarness,
) -> dict[str, Any]:
    registry = load_registry()
    with connect(db_path) as db:
        entry, binding = require_binding(ident, registry, db)
        if (ident not in CHILD_GOALS or entry["role"] != "parent"
                or not entry["configured_enabled"]
                or entry["mode"] != "observe_and_register"):
            raise HarnessError("only reviewed, enabled monitor parents may run this canary")
        immutable_goal = binding["main_goal"]
    store = HarnessStore(db_path)
    child_goal = CHILD_GOALS[ident]
    existing = [t for t in store.list_tasks(main_goal=immutable_goal)
                if t.child_goal == child_goal]
    if len(existing) > 1:
        raise HarnessError("duplicate canary checkpoint: manual review required")
    if existing:
        t = existing[0]
        return {
            "automation_id": ident, "harness_task_id": t.id, "state": t.state,
            "cleanup": t.cleanup_state, "repeat_launch_refused": True,
            "project_DoD": "NOT_TESTED_BY_CANARY",
        }
    if not launch:
        return {"automation_id": ident, "state": "NOT_LAUNCHED",
                "project_DoD": "NOT_TESTED_BY_CANARY"}
    manager = manager_cls(db_path, db_path.parent)
    result = manager.launch(
        main_goal=immutable_goal,
        child_goal=child_goal,
        expected=MARKERS[ident],
        repo=str(repo_path),
        executor="echo-probe", ttl_seconds=120,
    )
    return {
        "automation_id": ident, "state": result["state"],
        "harness_task_id": result["task_id"],
        "unit": result.get("unit"), "deadline": result.get("deadline"),
        "project_DoD": "NOT_TESTED_BY_CANARY",
        "note": "Systemd-owned echo output only; no project code, customer data or account actions",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor-id", required=True)
    parser.add_argument("--launch", action="store_true",
                        help="start one harmless systemd-owned echo-probe")
    parser.add_argument("--db", default=str(STATE_DB))
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    print("HARNESS_MONITOR_CANARY=" + json.dumps(
        run_one(args.monitor_id, Path(args.db), Path(args.repo), args.launch),
        ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
