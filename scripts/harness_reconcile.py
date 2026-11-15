"""Safe, one-shot Harness deadline reconciler and monitor-parent snapshot.

Run under the user's systemd manager on the same node as SQLite.
The --apply mode reconciles ONLY systemd units previously registered and owned
by this Harness database; it may stop an overdue owned unit, never arbitrary
sessions or unrelated services. systemd enforces hard RuntimeMaxSec by itself.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.agent_harness_mvp import HarnessStore
from harness_managed_exec import ManagedHarness

TERMINAL = {"DONE_VERIFIED", "FAILED", "EXPIRED"}


def snapshot(store: HarnessStore, now: datetime, *, main_goal: str | None = None):
    tasks = store.list_tasks(main_goal=main_goal)
    return {
        "main_goal": main_goal,
        "time": now.isoformat(),
        "children": [{
            "id": t.id, "goal": t.child_goal, "state": t.state,
            "agent": t.agent, "node": t.node, "expires_at": t.expires_at.isoformat(),
            "overdue": t.state not in TERMINAL and now >= t.expires_at,
            "cleanup": t.cleanup_state,
        } for t in tasks],
        "due_count": sum(t.state not in TERMINAL and now >= t.expires_at for t in tasks),
        "cleanup_pending": sum(t.cleanup_state == "CLEANUP_PENDING" for t in tasks),
        "parent_done": False,  # Never infer main-goal E2E PASS from children alone.
        "note": "No agent running state is inferred from a stored task.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get(
        "HARNESS_DB", str(Path.home()/".local/state/projectflow-harness/harness-mvp.db")))
    parser.add_argument("--main-goal")
    parser.add_argument("--apply", action="store_true",
                        help="Persist EXPIRED + CLEANUP_PENDING for overdue tasks only")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    db = Path(args.db).expanduser()
    if not db.is_file():
        raise SystemExit(f"REFUSED: database absent: {db}")
    store = HarnessStore(db)
    managed = []
    if args.apply:
        # The persisted ownership registry limits any stop/reset to a
        # Harness-created transient unit. Only then mark unresolved TTL.
        manager = ManagedHarness(db, db.parent)
        managed = manager.reconcile()
    newly_expired = store.reconcile_due(at=now) if args.apply else []
    result = snapshot(store, now, main_goal=args.main_goal)
    result["managed_units"] = managed
    result["mode"] = "APPLY" if args.apply else "READ_ONLY"
    result["new_expired_ids"] = [t.id for t in newly_expired]
    print("HARNESS_RECONCILE=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    if any("error" in item for item in managed):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
