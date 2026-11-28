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
