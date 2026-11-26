from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import PurePosixPath

import pytest

from app.services.node_capacity import (
    CapacityLease,
    CapacityMountIdentity,
    CapacityReceipt,
    CapacityTargetRequest,
    CapacityTargetReceipt,
    CapacityCapabilityEvidence,
    CapacityAdmissionRequest,
    NodeCapacityGate,
    RawFilesystemCounters,
    VerifiedCapacityReceipt,
)

NOW = datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc)


class FixtureVerifier:
    """Synthetic typed issuer for consumer tests; never a production feed."""

    def verify(self, receipt):
        if not isinstance(receipt, CapacityReceipt):
            raise TypeError("receipt must be typed")
        return VerifiedCapacityReceipt(receipt=receipt, issuer=self)


def mount(identity="dell-root"):
    return CapacityMountIdentity(
        boot_id_sha256=hashlib.sha256(f"boot-{identity}".encode()).hexdigest(), mount_namespace="mntns-1",
        kernel_mount_id="42", st_dev="dev-802", directory_inode=101,
        mountpoint="/home/dell/workspace", mount_root="/",
        fs_type="ext4", major_minor="8:2",
    )


def request(targets=None, *, requested_headroom_bytes=20 * 1024**3,
            task_id=31, node_id="dell-linux", workspace="/home/dell/workspace/.worktrees/task-31"):
    lease = CapacityLease(
        resource_key=f"task:{task_id}:workspace", owner_id="controller-7",
        acquired_at=NOW - timedelta(seconds=5), expires_at=NOW + timedelta(seconds=120),
    )
    return CapacityAdmissionRequest(
        schema_version=1, request_key=f"req-{task_id}-a", task_id=task_id,
        project_id="hungtranbkit/projectflow-workspace-manager",
        node_id=node_id, identity_revision="identity-r4",
        workspace=workspace,
        workspace_revision="a" * 40, lease=lease,
        targets=tuple(targets or (
            CapacityTargetRequest(
                role="worktree", path="/home/dell/workspace/.worktrees/task-31",
                expected_mount=mount(),
            ),
        )),
        required_capabilities=frozenset({"non_docker"}),
        requested_headroom_bytes=requested_headroom_bytes,
    )


def raw(*, bavail=30 * 1024**3, f_blocks=100, f_bfree=40,
        f_files=1000, f_ffree=700, f_favail=250):
    return RawFilesystemCounters(
        f_frsize=1024**3, f_blocks=f_blocks, f_bfree=f_bfree,
        f_bavail=bavail // (1024**3), f_files=f_files,
        f_ffree=f_ffree, f_favail=f_favail,
    )


def target_receipt(req, *, role="worktree", path=None, mount_identity=None, counters=None):
    selected = next(item for item in req.targets if item.role == role)
    requested_path = path or selected.path
    parent = str(PurePosixPath(requested_path).parent)
    suffix = PurePosixPath(requested_path).name
    return CapacityTargetReceipt(
        role=role, requested_path=requested_path,
        canonical_existing_ancestor=parent, relative_suffix=suffix,
        mount=mount_identity or selected.expected_mount,
        counters=counters or raw(),
    )


def receipt(req, *, targets=None, observed_at=NOW - timedelta(seconds=2),
            received_at=NOW - timedelta(seconds=1), expires_at=NOW + timedelta(seconds=60),
            lease=None, node_id=None, identity_revision=None, request_key=None,
            task_id=None, project_id=None, workspace=None, workspace_revision=None,
            capabilities=None, online=True, request_sha256=None):
    return CapacityReceipt(
        schema_version=1, receipt_id="receipt-31", request_key=request_key or req.request_key,
        request_sha256=request_sha256 or req.fingerprint(), task_id=req.task_id if task_id is None else task_id,
        project_id=project_id or req.project_id, node_id=node_id or req.node_id,
        identity_revision=identity_revision or req.identity_revision,
        workspace=workspace or req.workspace,
        workspace_revision=workspace_revision or req.workspace_revision,
        lease=lease or req.lease, controller_received_at=received_at,
        observed_at=observed_at, expires_at=expires_at,
        targets=tuple(targets or (target_receipt(req),)),
        capabilities=tuple(capabilities or (
            CapacityCapabilityEvidence("non_docker", "binary", "SUPPORTED", observed_at),
        )), online=online,
    )


def gate():
    return NodeCapacityGate(receipt_verifier=FixtureVerifier())


def typed_fixture_decision(gate_instance, paths, *, node_id, available_bytes):
    expected_mount = mount("test-local-target")
    targets = tuple(CapacityTargetRequest(f"target-{idx}", str(path), expected_mount)
                    for idx, path in enumerate(paths))
    req = request(targets, task_id=1, node_id=node_id,
                  workspace=str(paths[0]))
    evidence = tuple(target_receipt(
        req, role=target.role,
        counters=raw(bavail=available_bytes),
    ) for target in req.targets)
    return gate_instance.evaluate_receipt(
        req, receipt(req, targets=evidence), now=NOW, heavy=True,
    )


def test_verified_typed_receipt_matching_request_and_mount_is_evaluated_from_raw_counters():
    req = request()
    result = gate().evaluate_receipt(req, receipt(req), now=NOW, heavy=True)
    assert result.code == "ALLOW"
    assert result.node_id == "dell-linux"
    assert result.mount_id == mount().stable_id()
    assert result.free_bytes == 30 * 1024**3


@pytest.mark.parametrize("change, reason", [
    (lambda req, rec: replace(rec, request_key="other"), "CAPACITY_REQUEST_MISMATCH"),
    (lambda req, rec: replace(rec, request_sha256="0" * 64), "CAPACITY_REQUEST_HASH_MISMATCH"),
    (lambda req, rec: replace(rec, task_id=99), "CAPACITY_REQUEST_MISMATCH"),
    (lambda req, rec: replace(rec, project_id="other/project"), "CAPACITY_REQUEST_MISMATCH"),
    (lambda req, rec: replace(rec, node_id="hp-linux"), "CAPACITY_NODE_IDENTITY_MISMATCH"),
    (lambda req, rec: replace(rec, identity_revision="old"), "CAPACITY_NODE_IDENTITY_MISMATCH"),
    (lambda req, rec: replace(rec, workspace="/other/workspace"), "CAPACITY_REQUEST_MISMATCH"),
    (lambda req, rec: replace(rec, workspace_revision="b" * 40), "CAPACITY_REQUEST_MISMATCH"),
    (lambda req, rec: replace(rec, lease=replace(rec.lease, owner_id="other")), "CAPACITY_LEASE_MISMATCH"),
    (lambda req, rec: replace(rec, targets=(target_receipt(req, path="/wrong/path"),)), "CAPACITY_TARGET_MISMATCH"),
    (lambda req, rec: replace(rec, targets=(target_receipt(req, mount_identity=mount("other")),)), "CAPACITY_MOUNT_MISMATCH"),
])
def test_request_workspace_task_node_revision_mount_and_lease_mismatch_fail_closed(change, reason):
    req = request()
    result = gate().evaluate_receipt(req, change(req, receipt(req)), now=NOW, heavy=True)
    assert result.code == "BLOCKED_CAPACITY"
    assert result.reason == reason


@pytest.mark.parametrize("change, reason", [
    (lambda rec: replace(rec, observed_at=NOW + timedelta(seconds=1)), "CAPACITY_FUTURE"),
    (lambda rec: replace(rec, controller_received_at=NOW + timedelta(seconds=1)), "CAPACITY_FUTURE"),
    (lambda rec: replace(rec, expires_at=NOW - timedelta(seconds=1)), "CAPACITY_RECEIPT_EXPIRED"),
    (lambda rec: replace(rec, expires_at=rec.lease.expires_at + timedelta(seconds=1)), "CAPACITY_RECEIPT_EXPIRY_INVALID"),
    (lambda rec: replace(rec, online=False), "NODE_OFFLINE"),
    (lambda rec: replace(rec, capabilities=()), "CAPABILITY_MISMATCH"),
    (lambda rec: replace(rec, capabilities=(CapacityCapabilityEvidence("non_docker", "binary", "UNSUPPORTED", NOW),)), "CAPABILITY_MISMATCH"),
    (lambda rec: replace(rec, capabilities=(CapacityCapabilityEvidence("non_docker", "binary", "SUPPORTED", NOW + timedelta(seconds=1)),)), "CAPABILITY_MISMATCH"),
])
def test_stale_future_expired_offline_and_capability_mismatch_fail_closed(change, reason):
    req = request()
    result = gate().evaluate_receipt(req, change(receipt(req)), now=NOW, heavy=True)
    assert result.code == "BLOCKED_CAPACITY"
    assert result.reason == reason


def test_receipt_observation_older_than_ttl_is_stale_even_when_lease_predates_it():
    req = request()
    req = replace(req, lease=replace(req.lease, acquired_at=NOW - timedelta(seconds=400)))
    rec = receipt(req, observed_at=NOW - timedelta(seconds=301),
                  received_at=NOW - timedelta(seconds=300))
    result = gate().evaluate_receipt(req, rec, now=NOW, heavy=True)
    assert result.code == "BLOCKED_CAPACITY"
    assert result.reason == "CAPACITY_STALE"


@pytest.mark.parametrize("bad", [
    RawFilesystemCounters(1024, 10, 11, 1, 10, 2, 1),
    RawFilesystemCounters(1024, 10, 2, 3, 10, 2, 1),
    RawFilesystemCounters(1024, 10, 2, 1, 10, 2, 3),
    RawFilesystemCounters(1024, 10, 2, 1, 0, 0, 0),
])
def test_invalid_raw_statvfs_counters_fail_closed(bad):
    req = request()
    rec = receipt(req, targets=(target_receipt(req, counters=bad),))
    result = gate().evaluate_receipt(req, rec, now=NOW, heavy=True)
    assert result.code == "BLOCKED_CAPACITY"
    assert result.reason == "CAPACITY_METRICS_INVALID"


def test_zero_available_statvfs_is_valid_but_blocks_as_full_capacity():
    req = request()
    rec = receipt(req, targets=(target_receipt(req, counters=raw(bavail=0)),))
    result = gate().evaluate_receipt(req, rec, now=NOW, heavy=True)
    assert result.code == "BLOCKED_CAPACITY"
    assert result.reason == "CAPACITY_THRESHOLD_EXCEEDED"


def test_requested_headroom_and_any_red_mount_block_the_whole_admission():
    req = request(requested_headroom_bytes=40 * 1024**3)
    low = replace(target_receipt(req), counters=raw(bavail=30 * 1024**3))
    req_two = replace(req, targets=req.targets + (
        CapacityTargetRequest("cache", "/var/cache/projectflow", mount("cache-mount")),
    ))
    first = target_receipt(req_two, role="worktree")
    second = target_receipt(req_two, role="cache", counters=raw(bavail=10 * 1024**3))
    result = gate().evaluate_receipt(req_two, receipt(req_two, targets=(first, second)), now=NOW, heavy=True)
    assert result.code == "BLOCKED_CAPACITY"
    assert result.reason == "REQUESTED_HEADROOM_UNMET"


def test_missing_duplicate_or_extra_target_receipts_fail_closed():
    req = request(targets=(
        CapacityTargetRequest("worktree", "/workspace/w", mount("w")),
        CapacityTargetRequest("state", "/workspace/s", mount("s")),
    ))
    one = target_receipt(req, role="worktree")
    duplicate = target_receipt(req, role="worktree")
    gate_instance = gate()
    for targets in ((one,), (one, duplicate, target_receipt(req, role="state"))):
        result = gate_instance.evaluate_receipt(req, receipt(req, targets=targets), now=NOW, heavy=True)
        assert result.code == "BLOCKED_CAPACITY"
        assert result.reason == "CAPACITY_TARGETS_MISMATCH"


def test_expired_lease_and_observation_before_lease_acquisition_fail_closed():
    req = request()
    expired_lease = replace(req.lease, expires_at=NOW - timedelta(seconds=1))
    expired_req = replace(req, lease=expired_lease)
    expired_result = gate().evaluate_receipt(
        expired_req, receipt(expired_req), now=NOW, heavy=True,
    )
    assert expired_result.reason == "CAPACITY_LEASE_INVALID"

    too_new_lease = replace(req.lease, acquired_at=NOW - timedelta(seconds=1))
    too_new_req = replace(req, lease=too_new_lease)
    old_observation = receipt(
        too_new_req, observed_at=NOW - timedelta(seconds=2),
        received_at=NOW - timedelta(seconds=1),
    )
    result = gate().evaluate_receipt(too_new_req, old_observation,
                                     now=NOW, heavy=True)
    assert result.reason == "CAPACITY_LEASE_INVALID"


def test_unverified_caller_object_is_never_accepted_as_service_owned_provenance():
    req = request()
    result = NodeCapacityGate().evaluate_receipt(req, receipt(req), now=NOW, heavy=True)
    assert result.code == "BLOCKED_CAPACITY"
    assert result.reason == "CAPACITY_RECEIPT_VERIFIER_UNAVAILABLE"


def test_typed_blocking_fixture_stops_real_task_allocation_before_git_or_db_side_effects(
        client, git_repo, monkeypatch):
    _, repo = git_repo
    client.post("/api/repositories", data={
        "repo_path": str(repo), "repo_name": "receipt-capacity", "default_branch": "main"
    }, follow_redirects=False)
    repository_id = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Typed receipt boundary"}, follow_redirects=False)
    task_id = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{task_id}/select", follow_redirects=False)
    gate_instance = client.app.state.node_capacity_gate
    gate_instance._receipt_verifier = FixtureVerifier()
    git_calls = []
    monkeypatch.setattr(client.app.state.git, "create_agent",
                        lambda *args, **kwargs: git_calls.append((args, kwargs)))
    monkeypatch.setattr(gate_instance, "evaluate_paths", lambda paths, **kwargs:
                        typed_fixture_decision(gate_instance, paths,
                                               node_id=kwargs["node_id"],
                                               available_bytes=10 * 1024**3))
    before = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_workspaces")["n"]
    response = client.post(f"/api/tasks/{task_id}/workspaces", data={
        "repository_id": str(repository_id), "agent": "codex", "role": "Backend",
        "base_branch": "main", "sandbox_profile": "NONE",
    }, follow_redirects=False)
    after = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_workspaces")["n"]
    assert response.status_code == 409
    assert not git_calls
    assert after == before
    assert not (repo.parent / ".worktrees").exists()


def test_typed_capacity_fixture_allows_local_workspace_allocation_and_blocks_start_before_session(
        client, git_repo, monkeypatch):
    _, repo = git_repo
    client.post("/api/repositories", data={
        "repo_path": str(repo), "repo_name": "receipt-capacity-ok", "default_branch": "main"
    }, follow_redirects=False)
    repository_id = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Typed local eligibility"}, follow_redirects=False)
    task_id = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{task_id}/select", follow_redirects=False)
    gate_instance = client.app.state.node_capacity_gate
    gate_instance._receipt_verifier = FixtureVerifier()
    monkeypatch.setattr(gate_instance, "evaluate_paths", lambda paths, **kwargs:
                        typed_fixture_decision(gate_instance, paths,
                                               node_id=kwargs["node_id"],
                                               available_bytes=30 * 1024**3))
    created = client.post(f"/api/tasks/{task_id}/workspaces", data={
        "repository_id": str(repository_id), "agent": "codex", "role": "Backend",
        "base_branch": "main", "sandbox_profile": "NONE",
    }, follow_redirects=False)
    assert created.status_code == 303
    workspace = client.get(f"/api/tasks/{task_id}").json()["workspaces"][0]
    sessions = client.app.state.agent_sessions
    starts = []
    monkeypatch.setattr(sessions, "start", lambda *args, **kwargs:
                        starts.append((args, kwargs)))
    monkeypatch.setattr(gate_instance, "evaluate_paths", lambda paths, **kwargs:
                        typed_fixture_decision(gate_instance, paths,
                                               node_id=kwargs["node_id"],
                                               available_bytes=10 * 1024**3))
    before = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_sessions")["n"]
    response = client.post(f"/api/workspaces/{workspace['id']}/sessions",
                           data={"mode": "INTERACTIVE"}, follow_redirects=False)
    after = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_sessions")["n"]
    assert response.status_code == 409
    assert not starts
    assert after == before

