"""Safe no-network tests for ChatGPT automation ↔ Harness binding."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app.services.agent_harness_mvp import HarnessError, HarnessStore
from scripts import harness_monitor_bridge as bridge


@pytest.fixture
def source():
    return bridge.load_registry()


@pytest.fixture
def wired(tmp_path, source):
    db = tmp_path / "state.db"
    HarnessStore(db)  # existing live Harness schema must exist before bootstrap
    receipt = bridge.bootstrap(source, db)
    assert receipt["total"] == 25
    return db


def active_parent(source):
    return next((id for id, x in source.items()
                 if x["configured_enabled"] and x["role"] == "parent"))


def paused(source):
    return next((id for id, x in source.items() if not x["configured_enabled"]))


def backend(task_id="dt_proof_001", session="harness-demo", agent="codex",
            state="RUNNING", updated_at=None):
    return {
        "status": "OK",
        "result": {"status": "OK", "task": {
            "task_id": task_id, "target": session,
            "agent": agent, "mode": "agent", "state": state,
            "running_confirmed": True, "turn_state": state,
            "updated_at": (updated_at or datetime.now(timezone.utc).timestamp()),
            "watcher": {"alive": True},
        }},
    }


def test_inventories_all_existing_automations_without_enabling(source, wired):
    assert len(source) == 25
    assert sum(x["configured_enabled"] for x in source.values()) == 3
    assert sum(not x["configured_enabled"] for x in source.values()) == 22
    status = bridge.status(source, wired, None)
    assert status["count"] == 25
    assert status["active"] == 3
    assert status["paused"] == 22
    assert all(x["mode"] == "paused" and x["main_goal"] is None
               for x in status["monitors"] if not x["enabled_at_import"])


def test_bootstrap_reuse_and_immutable_main_goal(source, wired):
    assert bridge.bootstrap(source, wired) == {
        "registered": 0, "already": 25, "total": 25}
    chosen = active_parent(source)
    mod = {key: value.copy() for key, value in source.items()}
    mod[chosen]["main_goal"] = "silently changed project goal"
    with pytest.raises(HarnessError, match="immutable"):
        bridge.bootstrap(mod, wired)
    with pytest.raises(HarnessError, match="GOAL_DRIFT"):
        bridge.status(mod, wired, chosen)


def test_external_receipt_is_dispatched_only_and_idempotent(source, wired):
    ident = active_parent(source)
    opts = dict(
        registry=source, db_path=wired, ident=ident,
        child_goal="one small source audit", dod="one independent source test",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=1800,
        backend=backend(),
    )
    one = bridge.register(**opts)
    assert one["state"] == "DISPATCHED"
    assert one["RUNNING_VERIFIED"] is False
    duplicate = bridge.register(**opts)
    assert duplicate["task_id"] == one["task_id"]
    assert duplicate["idempotent"] is True
    status = bridge.status(source, wired, ident)
    child = status["monitors"][0]["child_tasks"][0]
    assert child["state"] == "DISPATCHED"
    assert child["external_task"] is True
    assert child["execution_backend"] == "LOCAL_MCP"


def test_wrong_target_and_paused_registration_are_denied(source, wired):
    ident = active_parent(source)
    kw = dict(
        registry=source, db_path=wired, ident=ident, child_goal="read-only test",
        dod="test output verified", agent="codex", node="dell-linux",
        session_name="harness-demo", task_id="dt_proof_001",
        ttl_seconds=900, backend=backend(session="another-session"),
    )
    with pytest.raises(HarnessError, match="target/agent"):
        bridge.register(**kw)
    kw["ident"] = paused(source)
    kw["backend"] = backend()
    with pytest.raises(HarnessError, match="paused/maintenance"):
        bridge.register(**kw)


def test_two_active_child_limit_enforced(source, wired):
    ident = active_parent(source)
    for number in (1, 2):
        bridge.register(
            registry=source, db_path=wired, ident=ident,
            child_goal=f"independent step {number}", dod="one test per step",
            agent="codex", node="dell-linux", session_name="harness-demo",
            task_id=f"dt_proof_{number:03d}", ttl_seconds=1800,
            backend=backend(task_id=f"dt_proof_{number:03d}"),
        )
    with pytest.raises(HarnessError, match="WIP limit"):
        bridge.register(
            registry=source, db_path=wired, ident=ident,
            child_goal="unexpected third task", dod="one test",
            agent="codex", node="dell-linux", session_name="harness-demo",
            task_id="dt_proof_003", ttl_seconds=1800,
            backend=backend(task_id="dt_proof_003"),
        )


def test_remote_DONE_remains_unaccepted_without_independent_DoD(source, wired, monkeypatch):
    ident = active_parent(source)
    task = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="external completed agent", dod="runtime E2E acceptance",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600,
        backend=backend(),
    )

    async def remote(_):
        return backend(state="DONE")

    monkeypatch.setattr(bridge, "verify_external_task", remote)
    result = asyncio.run(bridge.reconcile_external(source, wired))
    assert result["checked"] == 1
    assert result["results"][0]["task_state"] == "NOT_RUNNING"
    assert HarnessStore(wired).get(task["task_id"]).state != "DONE_VERIFIED"


def test_remote_running_requires_fresh_backend_progress(source, wired, monkeypatch):
    ident = active_parent(source)
    task = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="external still running", dod="one independent test",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600,
        backend=backend(),
    )

    async def remote(_):
        return backend()

    monkeypatch.setattr(bridge, "verify_external_task", remote)
    asyncio.run(bridge.reconcile_external(source, wired))
    assert HarnessStore(wired).get(task["task_id"]).state == "RUNNING_VERIFIED"


def test_backend_failure_does_not_lie_about_progress(source, wired, monkeypatch):
    ident = active_parent(source)
    task = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="unavailable backend", dod="test", agent="codex",
        node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600, backend=backend(),
    )

    async def offline(_):
        raise HarnessError("MCP offline")

    monkeypatch.setattr(bridge, "verify_external_task", offline)
    result = asyncio.run(bridge.reconcile_external(source, wired))
    assert result["results"][0]["backend_state"] == "UNKNOWN"
    assert HarnessStore(wired).get(task["task_id"]).state == "DISPATCHED"


def test_mcp_decoder_rejects_unstructured_or_error():
    from types import SimpleNamespace
    with pytest.raises(HarnessError):
        bridge.decode_mcp(SimpleNamespace(is_error=True))
    with pytest.raises(HarnessError):
        bridge.decode_mcp(SimpleNamespace(is_error=False, structured_content=None,
                                          content=[SimpleNamespace(text="not json")]))


def test_central_receipt_is_a_checkpoint_not_running_proof(source, wired):
    ident = active_parent(source)
    task = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="one global controller task", dod="runtime check",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600,
        backend=backend(), provenance="CENTRAL_ATTESTED",
    )
    assert task["state"] == "DISPATCHED"
    assert task["RUNNING_VERIFIED"] is False
    observed = bridge.observe_attested(source, wired, ident, "dt_proof_001",
                                        backend(state="RUNNING"))
    assert observed["state"] == "UNKNOWN"  # central JSON alone never proves process liveness
    observed = bridge.observe_attested(source, wired, ident, "dt_proof_001",
                                        backend(state="DONE"))
    assert observed["state"] == "NOT_RUNNING"
    assert observed["DoD"] == "REQUIRES_INDEPENDENT_ACCEPTANCE"
    assert HarnessStore(wired).get(task["task_id"]).state != "DONE_VERIFIED"
    summary = asyncio.run(bridge.reconcile_external(source, wired))
    assert summary["results"][0]["backend_state"] == "UNAVAILABLE_LOCAL"


def test_central_attested_failed_task_still_blocks_cleanup(source, wired):
    ident = active_parent(source)
    task = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="global task failed", dod="post-failure review",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600,
        backend=backend(), provenance="CENTRAL_ATTESTED",
    )
    outcome = bridge.observe_attested(source, wired, ident, "dt_proof_001",
                                      backend(state="FAILED"))
    assert outcome["state"] == "FAILED"
    assert outcome["cleanup"] == "CLEANUP_PENDING"
    assert bridge.status(source, wired, ident)["monitors"][0]["outstanding_children"] == 1


def test_attestation_wrong_session_is_refused(source, wired):
    ident = active_parent(source)
    task = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="central task", dod="one proof",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600,
        backend=backend(), provenance="CENTRAL_ATTESTED",
    )
    with pytest.raises(HarnessError, match="task/target"):
        bridge.observe_attested(source, wired, ident, "dt_proof_001",
                               backend(session="different"))
    assert HarnessStore(wired).get(task["task_id"]).state == "DISPATCHED"


def test_local_remote_failure_keeps_parent_wip_until_safe_cleanup(source, wired, monkeypatch):
    ident = active_parent(source)
    record = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="agent failed but session may remain", dod="independent runtime proof",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600,
        backend=backend(), provenance="LOCAL_MCP",
    )
    async def fail(_):
        return backend(state="FAILED")

    monkeypatch.setattr(bridge, "verify_external_task", fail)
    report = asyncio.run(bridge.reconcile_external(source, wired))
    task = HarnessStore(wired).get(record["task_id"])
    assert report["results"][0]["task_state"] == "FAILED"
    assert task.cleanup_state == "CLEANUP_PENDING"
    assert bridge.status(source, wired, ident)["monitors"][0]["outstanding_children"] == 1


def test_status_labels_internal_systemd_canary_separately(source, wired, tmp_path):
    from datetime import datetime, timezone
    from scripts.harness_managed_exec import ManagedHarness
    import sqlite3

    ident = active_parent(source)
    goal = source[ident]["main_goal"]
    managed = ManagedHarness(wired, tmp_path)
    task = HarnessStore(wired).create(
        key="owned-canary-probe", main_goal=goal,
        child_goal="CANARY ONLY: verify monitor-to-Harness receipt",
        dod="one technical echo proof", agent="shell",
        node="dell-linux", created_at=datetime.now(timezone.utc))
    with sqlite3.connect(wired) as sql:
        sql.execute("""INSERT INTO harness_managed_units
            (task_id,unit,run_dir,repo,executor,expected_sha,recorded_at)
            VALUES (?,?,?,?,?,?,?)""",
            (task.id, f"harness-child-{task.id}-1234567890.service",
             str(tmp_path / "run"), str(tmp_path / "repo"), "echo-probe",
             "proof", datetime.now(timezone.utc).isoformat()))
    view = bridge.status(source, wired, ident)["monitors"][0]["child_tasks"][0]
    assert view["execution_backend"] == "MANAGED_SYSTEMD"
    assert view["external_task"] is False
    assert view["project_DoD"] == "NOT_TESTED_BY_CANARY"


def test_monitor_status_releases_sqlite_fds_after_many_reads(source, wired):
    import os
    fd_path = Path("/proc/self/fd")
    if not fd_path.is_dir():
        pytest.skip("requires Linux /proc")
    before = len(os.listdir(fd_path))
    ident = active_parent(source)
    for _ in range(60):
        result = bridge.status(source, wired, ident)
        assert result["count"] == 1
    after = len(os.listdir(fd_path))
    assert after <= before + 8, (before, after)


@pytest.fixture
def reviewed_clone(tmp_path, source):
    import subprocess
    parent = tmp_path / "workspace"
    project = parent / "reviewed"
    project.mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "impl.py").write_text("def value(): return 1\n")
    (project / "tests/test_impl.py").write_text(
        "from impl import value\n\ndef test_value(): assert value() == 2\n")
    subprocess.run(["git", "-C", str(project), "init", "-b", "harness/qa"],
                   check=True, capture_output=True)
    ident = active_parent(source)
    subprocess.run(["git", "-C", str(project), "remote", "add", "origin",
                    f"https://github.com/{source[ident]['repo']}.git"],check=True)
    subprocess.run(["git", "-C", str(project), "add", "."],check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=QA",
                    "-c", "user.email=qa@localhost", "commit",
                    "-m", "test baseline"], check=True, capture_output=True)
    return parent, project


def test_direct_managed_dispatch_locked_to_origin_goal_and_readback(
    source, wired, reviewed_clone
):
    root, project = reviewed_clone
    chosen = active_parent(source)

    class FakeManager:
        seen = []
        def __init__(self, db, state):
            self.db = db
        def launch(self, **kwargs):
            self.seen.append(kwargs)
            return {"state": "DISPATCHED", "task_id": 700,
                    "unit": "harness-child-700-1234567890.service",
                    "deadline": "2026-11-17T00:00:00+00:00"}

    out = bridge.launch_managed_child(
        source, wired, chosen,
        child_goal="Fix implementation and pass preexisting test",
        repo_dir=project, test_path="tests/test_impl.py",
        allowed_files=["impl.py"], ttl_seconds=600,
        workspace_root=root, manager_type=FakeManager,
    )
    assert out["state"] == "DISPATCHED"
    assert out["durable_origin"] == "HARNESS_SQLITE"
    assert out["independent_project_DoD"] == "NOT_VERIFIED_BY_DISPATCH"
    assert out["execution_backend"] == "MANAGED_SYSTEMD"
    assert FakeManager.seen[0]["main_goal"] == source[chosen]["main_goal"]
    assert FakeManager.seen[0]["allowed_files"] == ["impl.py"]
    assert FakeManager.seen[0]["ttl_seconds"] == 600


def test_direct_managed_dispatch_refuses_wrong_project_origin(
    source, wired, reviewed_clone
):
    import subprocess
    root, project = reviewed_clone
    chosen = active_parent(source)
    subprocess.run(["git", "-C", str(project), "remote", "set-url",
                    "origin", "https://github.com/attacker/not-the-project.git"], check=True)
    with pytest.raises(HarnessError, match="project origin"):
        bridge.launch_managed_child(
            source, wired, chosen, child_goal="Unrelated coding work",
            repo_dir=project, test_path="tests/test_impl.py",
            allowed_files=["impl.py"], workspace_root=root)
    assert not HarnessStore(wired).list_tasks()


def test_direct_managed_dispatch_refuses_paused_and_maintenance(
    source, wired, reviewed_clone
):
    root, project = reviewed_clone
    blocked = [key for key, row in source.items()
               if not row["configured_enabled"] or row["role"] != "parent"]
    for ident in blocked:
        with pytest.raises(HarnessError, match="paused/maintenance"):
            bridge.launch_managed_child(
                source, wired, ident, child_goal="Prohibited child",
                repo_dir=project, test_path="tests/test_impl.py",
                allowed_files=["impl.py"], workspace_root=root)
    assert not HarnessStore(wired).list_tasks()


def test_direct_managed_dispatch_requires_clean_trusted_workspace(
    source, wired, reviewed_clone
):
    root, project = reviewed_clone
    chosen = active_parent(source)
    with pytest.raises(HarnessError, match="approved workspace"):
        bridge.launch_managed_child(
            source, wired, chosen, child_goal="Outside workspace",
            repo_dir=project, test_path="tests/test_impl.py",
            allowed_files=["impl.py"], workspace_root=root / "invalid")
    with pytest.raises(HarnessError, match="source allowlist"):
        bridge.launch_managed_child(
            source, wired, chosen, child_goal="No source approval",
            repo_dir=project, test_path="tests/test_impl.py",
            allowed_files=[], workspace_root=root)
    assert not HarnessStore(wired).list_tasks()
