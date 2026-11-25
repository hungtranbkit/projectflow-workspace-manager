from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.node_capacity import (CapacityPolicy, CapacitySnapshot, NodeCapacityGate,
                                        WorktreeCleanupEvidence, assess_worktree_cleanup)

NOW = datetime(2026, 11, 25, 4, 0, tzinfo=timezone.utc)
GIB = 1024 ** 3


def snap(node="dell-linux", *, used=70, free=100*GIB, inode=20, caps=(), at=NOW, online=True):
    return CapacitySnapshot(
        node_id=node, mount_id=f"dev-{node}", total_bytes=1000*GIB,
        free_bytes=free, used_percent=used, total_inodes=1000,
        free_inodes=1000 - inode*10, inode_used_percent=inode,
        observed_at=at, capabilities=frozenset(caps), online=online,
    )


def test_blocks_at_inclusive_used_and_inode_thresholds_and_below_minimum_free():
    gate = NodeCapacityGate(CapacityPolicy())
    assert gate.evaluate(snap(used=90), now=NOW, heavy=True).code == "BLOCKED_CAPACITY"
    assert gate.evaluate(snap(inode=90), now=NOW, heavy=True).code == "BLOCKED_CAPACITY"
    assert gate.evaluate(snap(free=20*GIB - 1), now=NOW, heavy=True).code == "BLOCKED_CAPACITY"
    assert gate.evaluate(snap(free=20*GIB), now=NOW, heavy=True).code == "ALLOW"


def test_warn_band_ranks_lower_without_blocking():
    result = NodeCapacityGate(CapacityPolicy()).evaluate(snap(used=80), now=NOW, heavy=True)
    assert result.code == "WARN_CAPACITY"
    assert result.rank_penalty > 0


def test_stale_offline_and_invalid_metrics_fail_closed_for_heavy_work():
    gate = NodeCapacityGate(CapacityPolicy(freshness_ttl_seconds=300))
    assert gate.evaluate(snap(at=NOW - timedelta(seconds=301)), now=NOW, heavy=True).code == "BLOCKED_CAPACITY"
    assert gate.evaluate(snap(online=False), now=NOW, heavy=True).code == "BLOCKED_CAPACITY"
    assert gate.evaluate(snap(free=-1), now=NOW, heavy=True).code == "BLOCKED_CAPACITY"
    assert gate.evaluate(snap(at=NOW - timedelta(days=1)), now=NOW, heavy=False).code == "UNKNOWN_CAPACITY"


def test_hysteresis_keeps_block_until_clear_band_is_reached():
    gate = NodeCapacityGate(CapacityPolicy(clear_used_percent=85, clear_inode_used_percent=85,
                                           clear_free_bytes=24*GIB))
    still_blocked = gate.evaluate(snap(used=86, free=30*GIB), now=NOW, heavy=True, previously_blocked=True)
    assert still_blocked.code == "BLOCKED_CAPACITY"
    cleared = gate.evaluate(snap(used=84, free=24*GIB, inode=84), now=NOW,
                            heavy=True, previously_blocked=True)
    assert cleared.code == "WARN_CAPACITY"


def test_pinned_target_never_silently_reroutes():
    gate = NodeCapacityGate(CapacityPolicy())
    result = gate.select_node([snap("macbook", used=95, caps={"non_docker"}),
                               snap("dell-linux", caps={"non_docker"})],
                              now=NOW, required_capabilities={"non_docker"},
                              pinned_node="macbook", heavy=True)
    assert result.code == "BLOCKED_CAPACITY"
    assert result.node_id == "macbook"
    assert result.rerouted is False


def test_auto_selection_respects_capability_and_preferred_node_order():
    gate = NodeCapacityGate(CapacityPolicy())
    nodes = [snap("m910", caps={"docker"}), snap("dell-linux", caps={"docker"}),
             snap("macbook", caps={"non_docker"})]
    non_docker = gate.select_node(nodes, now=NOW, required_capabilities={"non_docker"}, heavy=True)
    docker = gate.select_node(nodes, now=NOW, required_capabilities={"docker"}, heavy=True)
    assert non_docker.node_id == "macbook"
    assert docker.node_id == "dell-linux"


def test_auto_selection_ranks_healthy_fallback_above_warned_preferred_node():
    gate = NodeCapacityGate(CapacityPolicy())
    nodes = [snap("macbook", used=80, caps={"non_docker"}),
             snap("dell-linux", used=70, caps={"non_docker"})]
    selected = gate.select_node(nodes, now=NOW, required_capabilities={"non_docker"}, heavy=True)
    assert selected.node_id == "dell-linux"
    assert selected.code == "ALLOW"


def test_filesystem_probe_reports_real_target_metrics(tmp_path):
    gate = NodeCapacityGate(CapacityPolicy())
    snapshot = gate.probe_path(tmp_path, node_id="test-node", capabilities={"non_docker"}, now=NOW)
    assert snapshot.node_id == "test-node"
    assert snapshot.mount_id
    assert snapshot.total_bytes > 0 and snapshot.free_bytes >= 0
    assert snapshot.total_inodes > 0 and snapshot.inode_used_percent >= 0
    assert snapshot.observed_at == NOW


def test_hysteresis_restores_latest_mount_decision_from_audit_events():
    gate = NodeCapacityGate()
    gate.restore_hysteresis([
        {"details": '{"node_id":"dell-linux","mount_id":"dev-1","code":"BLOCKED_CAPACITY"}'},
        {"details": '{"node_id":"dell-linux","mount_id":"dev-1","code":"WARN_CAPACITY"}'},
    ])
    assert ("dell-linux", "dev-1") not in gate._blocked_mounts
    gate.restore_hysteresis([
        {"details": '{"node_id":"dell-linux","mount_id":"dev-1","code":"WARN_CAPACITY"}'},
        {"details": '{"node_id":"dell-linux","mount_id":"dev-1","code":"BLOCKED_CAPACITY"}'},
    ])
    assert ("dell-linux", "dev-1") in gate._blocked_mounts


def test_cleanup_assessment_requires_all_proofs_and_never_deletes():
    old = NOW - timedelta(days=8)
    eligible = assess_worktree_cleanup(WorktreeCleanupEvidence(
        registered=True, branch="agent/example/task", clean=True, merged=True,
        last_activity_at=old, active_session=False, process_or_open_file=False,
        production_or_deploy_reference=False), now=NOW)
    assert eligible.eligible and eligible.dry_run and not eligible.removed
    denied = assess_worktree_cleanup(WorktreeCleanupEvidence(
        registered=True, branch="main", clean=None, merged=True,
        last_activity_at=None, active_session=None, process_or_open_file=None,
        production_or_deploy_reference=None), now=NOW)
    assert not denied.eligible and not denied.removed
    assert {"PROTECTED_OR_UNKNOWN_BRANCH", "WORKTREE_NOT_PROVEN_CLEAN",
            "ACTIVITY_TIME_UNKNOWN", "SESSION_STATE_NOT_CLEAR"}.issubset(denied.reasons)
