"""B05 shared-database admission contract.

The two managers below stand in for two independent service processes.  They
share only the SQLite database; no in-process lock is involved.  The test
therefore proves the capacity decision and operation row are one transaction,
and that retrying the same sandbox ID does not create a second operation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.services.sandbox_manager import SandboxError, SandboxManager


def _sandbox(db, slug: str) -> int:
    return db.execute(
        "INSERT INTO sandboxes(task_id,repository_id,owner_type,owner_id,sandbox_slug,profile,runtime_type,compose_project,status) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (None, None, "AGENT_WORKSPACE", 1, slug, "NONE", "docker-compose", slug, "CREATED"),
    )


def test_independent_managers_atomically_admit_one_and_idempotently_retry(client, tmp_path):
    db = client.app.state.db
    first = _sandbox(db, "b05-independent-a")
    second = _sandbox(db, "b05-independent-b")
    managers = [
        SandboxManager(db, client.app.state.sandbox_runtime, client.app.state.ports, tmp_path / "a", max_running=1),
        SandboxManager(db, client.app.state.sandbox_runtime, client.app.state.ports, tmp_path / "b", max_running=1),
    ]
    barrier = Barrier(2)

    def admit(manager: SandboxManager, sandbox_id: int):
        barrier.wait(timeout=5)
        try:
            return ("admitted", manager._admit_provision(sandbox_id))
        except SandboxError as exc:
            return (exc.code, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, managers, (first, second)))

    assert sorted(code for code, _ in results) == ["SANDBOX_CAPACITY_FULL", "admitted"]
    admitted_id = next(op for code, op in results if code == "admitted")
    assert admitted_id is not None
    assert db.one("SELECT COUNT(*) AS n FROM sandbox_operations WHERE operation_type='PROVISION'")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM sandboxes WHERE status='PROVISIONING'")["n"] == 1

    # A retry from the other independent manager observes the committed state
    # and is a no-op: no duplicate operation or retry spawn is possible.
    admitted_sandbox = first if db.one("SELECT status FROM sandboxes WHERE id=?", (first,))["status"] == "PROVISIONING" else second
    assert managers[1]._admit_provision(admitted_sandbox) is None
    assert db.one("SELECT COUNT(*) AS n FROM sandbox_operations WHERE operation_type='PROVISION'")["n"] == 1


def test_missing_sandbox_is_rejected_inside_admission_transaction(client, tmp_path):
    manager = SandboxManager(client.app.state.db, client.app.state.sandbox_runtime, client.app.state.ports, tmp_path, max_running=1)
    with pytest.raises(SandboxError, match="sandbox not found") as exc:
        manager._admit_provision(999999)
    assert exc.value.code == "SANDBOX_NOT_FOUND"


def test_actual_volume_headroom_rejection_is_durable_and_non_mutating(client, tmp_path):
    db = client.app.state.db
    sandbox_id = _sandbox(db, "b05-low-volume")
    manager = SandboxManager(db, client.app.state.sandbox_runtime, client.app.state.ports, tmp_path, max_running=1, reserve_bytes=100, reserve_inodes=10)
    manager.volume_capacity = lambda path: {"path": str(path), "free_bytes": 99, "free_inodes": 1000}
    with pytest.raises(SandboxError, match="free bytes") as exc:
        manager._admit_provision(sandbox_id)
    assert exc.value.code == "SANDBOX_DISK_HEADROOM"
    row = db.one("SELECT status,error_code FROM sandboxes WHERE id=?", (sandbox_id,))
    assert row == {"status": "FAILED", "error_code": "SANDBOX_DISK_HEADROOM"}
    event = db.one("SELECT action,details FROM workspace_events WHERE entity_id=? ORDER BY id DESC LIMIT 1", (sandbox_id,))
    assert event["action"] == "SANDBOX_ADMISSION_REJECTED"
    assert db.one("SELECT COUNT(*) n FROM sandbox_operations WHERE sandbox_id=?", (sandbox_id,))["n"] == 0


def test_stale_provision_lease_is_fenced_before_single_recovery_reservation(client, tmp_path):
    db = client.app.state.db
    sandbox_id = _sandbox(db, "b05-stale")
    db.execute("UPDATE sandboxes SET status='PROVISIONING' WHERE id=?", (sandbox_id,))
    db.execute("INSERT INTO sandbox_operations(sandbox_id,operation_type,status,started_at) VALUES(?,?,?,datetime('now','-2 hours'))", (sandbox_id, "PROVISION", "RUNNING"))
    manager = SandboxManager(db, client.app.state.sandbox_runtime, client.app.state.ports, tmp_path, max_running=1)
    op = manager._admit_provision(sandbox_id)
    assert op is not None
    assert db.one("SELECT COUNT(*) n FROM sandbox_operations WHERE sandbox_id=?", (sandbox_id,))["n"] == 2
    assert db.one("SELECT status FROM sandbox_operations WHERE sandbox_id=? ORDER BY id LIMIT 1", (sandbox_id,))["status"] == "FAILED"
