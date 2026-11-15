"""CLI acceptance: overdue work is marked, not falsely reported stopped."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from pathlib import Path

from app.services.agent_harness_mvp import HarnessStore

ROOT = Path(__file__).resolve().parents[1]


def _invoke(db, *options):
    process = subprocess.run(
        [sys.executable, str(ROOT / "scripts/harness_reconcile.py"), "--db", str(db), *options],
        check=True, capture_output=True, text=True, timeout=12,
    )
    line = [x for x in process.stdout.splitlines() if x.startswith("HARNESS_RECONCILE=")]
    assert len(line) == 1
    return json.loads(line[0].split("=", 1)[1])


def test_parent_snapshot_and_delayed_expiry_are_fail_closed(tmp_path):
    db = tmp_path / "harness.db"
    store = HarnessStore(db)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    task = store.create(
        key="scheduled-test", main_goal="immutable parent P0",
        child_goal="one expired small task", dod="artifact exists",
        agent="codex", node="dell-linux", created_at=old, ttl=timedelta(minutes=15),
    )
    before = _invoke(db, "--main-goal", task.main_goal)
    assert before["parent_done"] is False
    assert before["due_count"] == 1
    assert before["children"][0]["state"] == "NOT_DISPATCHED"
    assert store.get(task.id).state == "NOT_DISPATCHED"

    after = _invoke(db, "--main-goal", task.main_goal, "--apply")
    assert after["new_expired_ids"] == [task.id]
    assert after["due_count"] == 0
    assert after["cleanup_pending"] == 1
    assert after["children"][0]["state"] == "EXPIRED"
    assert after["children"][0]["cleanup"] == "CLEANUP_PENDING"
    assert after["parent_done"] is False
    # Scheduler repeats must not emit additional EXPIRED events.
    repeated = _invoke(db, "--main-goal", task.main_goal, "--apply")
    assert repeated["new_expired_ids"] == []
    assert sum(e["event"] == "EXPIRED" for e in store.events(task.id)) == 1
