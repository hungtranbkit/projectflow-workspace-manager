from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.node_capacity import (CapacityPolicy, CapacitySnapshot, NodeCapacityGate,
                                        WorktreeCleanupEvidence, assess_worktree_cleanup)

NOW = datetime(2026, 11, 25, 4, 0, tzinfo=timezone.utc)
GIB = 1024 ** 3


def snap(node="dell-linux", *, used=70, free=100*GIB, inode=20, caps=(), at=NOW, online=True):
    return CapacitySnapshot(
        node_id=node, mount_id=f"dev-{node}", total_bytes=int(free / (1 - used / 100)),
        used_bytes=int(free / (1 - used / 100)) - free,
        free_bytes=free, used_percent=used, total_inodes=1000,
        used_inodes=inode*10, free_inodes=1000 - inode*10, inode_used_percent=inode,
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
    inconsistent = snap(used=20, free=100*GIB)
    inconsistent = CapacitySnapshot(**{**inconsistent.__dict__, "used_percent": 70})
    assert gate.evaluate(inconsistent, now=NOW, heavy=True).code == "BLOCKED_CAPACITY"
    bad_inode = snap(inode=20)
    bad_inode = CapacitySnapshot(**{**bad_inode.__dict__, "free_inodes": 1})
    assert gate.evaluate(bad_inode, now=NOW, heavy=True).code == "BLOCKED_CAPACITY"
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


def test_issue86_reported_fleet_boundaries_are_policy_fixtures_not_live_health():
    """Replay reported HP/M910/MacBook/Dell boundaries, not live health.

    The HP/MacBook/Dell values mirror historical reports; the M910 92% is
    the owner-requested boundary. Free bytes are synthetic for HP/M910 so
    this specifically exercises percentage thresholds. None attest live health.
    """
    gate = NodeCapacityGate(CapacityPolicy())
    fleet = [
        snap("hp-linux", used=95, free=30*GIB, inode=16,
             caps={"non_docker", "docker"}),
        snap("m910", used=92, free=30*GIB, inode=17,
             caps={"non_docker", "docker"}),
        snap("macbook", used=18, free=385_339_617_280, inode=20,
             caps={"non_docker"}),
        snap("dell-linux", used=73, free=127_906_332_672, inode=14,
             caps={"non_docker", "docker"}),
    ]

    per_node = {item.node_id: gate.evaluate(item, now=NOW, heavy=True) for item in fleet}
    assert per_node["hp-linux"].code == "BLOCKED_CAPACITY"
    assert per_node["m910"].code == "BLOCKED_CAPACITY"
    assert per_node["m910"].used_percent == pytest.approx(92)
    assert per_node["macbook"].code == "ALLOW"
    assert per_node["dell-linux"].code == "ALLOW"

    non_docker = gate.select_node(fleet, now=NOW, required_capabilities={"non_docker"}, heavy=True)
    docker = gate.select_node(fleet, now=NOW, required_capabilities={"docker"}, heavy=True)
    assert (non_docker.node_id, non_docker.code) == ("macbook", "ALLOW")
    assert (docker.node_id, docker.code) == ("dell-linux", "ALLOW")


def test_issue86_exact_reported_root_tuples_replay_df_ratio_only_as_fixtures():
    """Replay the owner's exact root tuples; these do not attest target mounts."""
    gate = NodeCapacityGate(CapacityPolicy())
    # (used bytes, available bytes, reported inode Use%).  The filesystem
    # percent is df's rounded display; use the exact used/(used+available)
    # formula for policy, and preserve the supplied inode percentages as
    # explicitly synthetic counters.
    reported = {
        "hp-linux": (223_421_739_008, 13_607_075_840, 16),
        "m910": (107_810_967_552, 10_125_598_720, 17),
        "dell-linux": (369_241_120_768, 122_332_127_232, 15),
        "macbook": (80_966_119_424, 384_224_636_928, 3),
        "nec1": (44_738_027_520, 13_703_180_288, 12),
    }
    snapshots = []
    for node, (used, available, inode_percent) in reported.items():
        inode_total = 10_000
        inode_used = inode_total * inode_percent // 100
        snapshots.append(CapacitySnapshot(
            node_id=node, mount_id=f"reported-root-{node}",
            total_bytes=used + available, used_bytes=used,
            free_bytes=available,
            used_percent=100.0 * used / (used + available),
            total_inodes=inode_total, used_inodes=inode_used,
            free_inodes=inode_total - inode_used,
            inode_used_percent=100.0 * inode_used / inode_total,
            observed_at=NOW, capabilities=frozenset({"non_docker", "docker"}),
        ))

    by_node = {item.node_id: gate.evaluate(item, now=NOW, heavy=True)
               for item in snapshots}
    assert by_node["hp-linux"].used_percent == pytest.approx(
        100 * 223_421_739_008 / (223_421_739_008 + 13_607_075_840))
    assert by_node["m910"].used_percent == pytest.approx(
        100 * 107_810_967_552 / (107_810_967_552 + 10_125_598_720))
    assert by_node["hp-linux"].code == "BLOCKED_CAPACITY"
    assert by_node["m910"].code == "BLOCKED_CAPACITY"
    assert by_node["dell-linux"].code == "ALLOW"
    assert by_node["macbook"].code == "ALLOW"
    # NEC's exact available bytes are below the 20 GiB floor, so it blocks
    # despite a moderate percentage. This is the independent headroom guard.
    assert by_node["nec1"].code == "BLOCKED_CAPACITY"
    # This only exercises policy against the supplied root figures. Freshness,
    # capability, and mount identity still have to be proven by a live feed.
    assert gate.select_node(
        snapshots, now=NOW, required_capabilities={"non_docker"}, heavy=True
    ).node_id == "macbook"
    assert gate.select_node(
        snapshots, now=NOW, required_capabilities={"docker"}, heavy=True
    ).node_id == "dell-linux"


def test_fleet_feed_unknown_offline_and_pinned_capacity_fail_closed():
    gate = NodeCapacityGate(CapacityPolicy(freshness_ttl_seconds=300))
    stale_mac = snap("macbook", used=18, caps={"non_docker"},
                     at=NOW - timedelta(hours=1))
    offline_mac = snap("macbook", used=18, caps={"non_docker"}, online=False)

    pinned = gate.select_node([stale_mac, snap("dell-linux", caps={"non_docker"})],
                              now=NOW, required_capabilities={"non_docker"},
                              pinned_node="macbook", heavy=True)
    assert pinned.code == "BLOCKED_CAPACITY"
    assert pinned.node_id == "macbook"
    assert not pinned.rerouted

    blocked = gate.select_node([offline_mac], now=NOW,
                               required_capabilities={"non_docker"}, heavy=True)
    assert blocked.code == "BLOCKED_CAPACITY"
    assert blocked.node_id is None


def test_filesystem_probe_reports_real_target_metrics(tmp_path):
    gate = NodeCapacityGate(CapacityPolicy())
    snapshot = gate.probe_path(tmp_path, node_id="test-node", capabilities={"non_docker"}, now=NOW)
    assert snapshot.node_id == "test-node"
    assert snapshot.mount_id
    assert snapshot.total_bytes > 0 and snapshot.free_bytes >= 0
    assert snapshot.total_inodes > 0 and snapshot.inode_used_percent >= 0
    assert snapshot.observed_at == NOW


def test_filesystem_probe_matches_df_used_over_used_plus_available_with_reserved_blocks(tmp_path, monkeypatch):
    block_size = 4096
    stats = SimpleNamespace(
        f_frsize=block_size, f_bsize=block_size,
        f_blocks=1000, f_bfree=200, f_bavail=100,
        f_files=1000, f_ffree=200, f_favail=100,
    )
    monkeypatch.setattr("app.services.node_capacity.os.statvfs", lambda _path: stats)

    gate = NodeCapacityGate(CapacityPolicy(block_free_bytes=1, clear_free_bytes=1))
    snapshot = gate.probe_path(tmp_path, node_id="reserved-test", now=NOW)

    assert snapshot.total_bytes == 1000 * block_size
    assert snapshot.free_bytes == 100 * block_size
    assert snapshot.used_bytes == 800 * block_size
    assert snapshot.used_percent == pytest.approx(800 / (800 + 100) * 100)
    assert snapshot.used_inodes == 800
    assert snapshot.inode_used_percent == pytest.approx(800 / (800 + 100) * 100)
    decision = gate.evaluate(snapshot, now=NOW, heavy=True)
    assert decision.code == "WARN_CAPACITY"
    assert decision.audit_fields()["used_bytes"] == snapshot.used_bytes
    assert decision.audit_fields()["total_bytes"] == snapshot.total_bytes
    assert decision.audit_fields()["used_inodes"] == snapshot.used_inodes


def test_filesystem_probe_rejects_zero_or_impossible_block_counters(tmp_path, monkeypatch):
    gate = NodeCapacityGate(CapacityPolicy())
    for stats in (
        SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=0, f_bfree=0, f_bavail=0,
                        f_files=100, f_ffree=10, f_favail=10),
        SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=10, f_bfree=11, f_bavail=1,
                        f_files=100, f_ffree=10, f_favail=10),
        SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=10, f_bfree=2, f_bavail=3,
                        f_files=100, f_ffree=10, f_favail=10),
    ):
        monkeypatch.setattr("app.services.node_capacity.os.statvfs", lambda _path, value=stats: value)
        with pytest.raises(ValueError, match="filesystem block metrics are invalid"):
            gate.probe_path(tmp_path, node_id="invalid-test", now=NOW)


def test_filesystem_probe_rejects_zero_inode_counters(tmp_path, monkeypatch):
    stats = SimpleNamespace(
        f_frsize=4096, f_bsize=4096, f_blocks=1000, f_bfree=200, f_bavail=100,
        f_files=0, f_ffree=0, f_favail=0,
    )
    monkeypatch.setattr("app.services.node_capacity.os.statvfs", lambda _path: stats)
    with pytest.raises(ValueError, match="filesystem inode metrics are invalid"):
        NodeCapacityGate().probe_path(tmp_path, node_id="invalid-inode-test", now=NOW)


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
