"""Fresh filesystem capacity checks used before resource-heavy dispatch.

The service is pure apart from probe_path(); callers own trusted target paths
and supply current node capabilities. It never starts work or deletes data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

GIB = 1024 ** 3


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("capacity timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CapacityPolicy:
    block_used_percent: float = 90.0
    block_free_bytes: int = 20 * GIB
    block_inode_used_percent: float = 90.0
    warn_used_percent: float = 80.0
    warn_inode_used_percent: float = 80.0
    freshness_ttl_seconds: int = 300
    clear_used_percent: float = 85.0
    clear_inode_used_percent: float = 85.0
    clear_free_bytes: int = 24 * GIB
    preference_non_docker: tuple[str, ...] = ("macbook", "dell-linux")
    preference_docker: tuple[str, ...] = ("dell-linux",)

    def __post_init__(self):
        if not (0 < self.warn_used_percent < self.block_used_percent <= 100):
            raise ValueError("used thresholds must satisfy 0 < warn < block <= 100")
        if not (0 < self.warn_inode_used_percent < self.block_inode_used_percent <= 100):
            raise ValueError("inode thresholds must satisfy 0 < warn < block <= 100")
        if not (0 <= self.clear_used_percent < self.block_used_percent):
            raise ValueError("clear used threshold must be below block threshold")
        if not (0 <= self.clear_inode_used_percent < self.block_inode_used_percent):
            raise ValueError("clear inode threshold must be below block threshold")
        if self.block_free_bytes <= 0 or self.clear_free_bytes < self.block_free_bytes:
            raise ValueError("clear free bytes must be >= positive block free bytes")
        if self.freshness_ttl_seconds <= 0:
            raise ValueError("freshness TTL must be positive")


@dataclass(frozen=True)
class CapacitySnapshot:
    node_id: str
    mount_id: str
    total_bytes: int
    free_bytes: int
    used_percent: float
    total_inodes: int
    free_inodes: int
    inode_used_percent: float
    observed_at: datetime
    capabilities: frozenset[str] = field(default_factory=frozenset)
    online: bool = True


@dataclass(frozen=True)
class CapacityDecision:
    code: str
    node_id: str | None
    mount_id: str | None
    reason: str
    observed_at: datetime | None
    free_bytes: int | None
    used_percent: float | None
    inode_used_percent: float | None
    rank_penalty: int = 0
    rerouted: bool = False

    @property
    def allowed(self) -> bool:
        return self.code in {"ALLOW", "WARN_CAPACITY"}

    def audit_fields(self) -> dict:
        return {
            "code": self.code, "node_id": self.node_id, "mount_id": self.mount_id,
            "reason": self.reason, "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "free_bytes": self.free_bytes, "used_percent": self.used_percent,
            "inode_used_percent": self.inode_used_percent,
            "rank_penalty": self.rank_penalty, "rerouted": self.rerouted,
        }


@dataclass(frozen=True)
class WorktreeCleanupEvidence:
    registered: bool
    branch: str | None
    clean: bool | None
    merged: bool | None
    last_activity_at: datetime | None
    active_session: bool | None
    process_or_open_file: bool | None
    production_or_deploy_reference: bool | None


@dataclass(frozen=True)
class CleanupAssessment:
    eligible: bool
    reasons: tuple[str, ...]
    dry_run: bool = True
    removed: bool = False


def assess_worktree_cleanup(evidence: WorktreeCleanupEvidence, *, now: datetime,
                            minimum_inactive_days: int = 7) -> CleanupAssessment:
    """Return proof-based eligibility only; this function never removes anything."""
    reasons = []
    if not evidence.registered:
        reasons.append("NOT_REGISTERED")
    if not evidence.branch or evidence.branch.strip().lower() in {"main", "master"}:
        reasons.append("PROTECTED_OR_UNKNOWN_BRANCH")
    if evidence.clean is not True:
        reasons.append("WORKTREE_NOT_PROVEN_CLEAN")
    if evidence.merged is not True:
        reasons.append("BRANCH_NOT_PROVEN_MERGED")
    if evidence.last_activity_at is None:
        reasons.append("ACTIVITY_TIME_UNKNOWN")
    else:
        try:
            inactive = _utc(now) - _utc(evidence.last_activity_at)
            if inactive < timedelta(days=minimum_inactive_days):
                reasons.append("INACTIVE_PERIOD_TOO_SHORT")
        except (TypeError, ValueError):
            reasons.append("ACTIVITY_TIME_INVALID")
    if evidence.active_session is not False:
        reasons.append("SESSION_STATE_NOT_CLEAR")
    if evidence.process_or_open_file is not False:
        reasons.append("PROCESS_OR_OPEN_FILE_STATE_NOT_CLEAR")
    if evidence.production_or_deploy_reference is not False:
        reasons.append("PRODUCTION_DEPLOY_REFERENCE_NOT_CLEAR")
    return CleanupAssessment(not reasons, tuple(reasons))


class NodeCapacityGate:
    def __init__(self, policy: CapacityPolicy | None = None):
        self.policy = policy or CapacityPolicy()
        self._blocked_mounts: set[tuple[str, str]] = set()

    def restore_hysteresis(self, events) -> None:
        """Restore mount state from append-only capacity audit events."""
        latest = {}
        for event in events:
            try:
                payload = json.loads(event["details"])
                key = (payload["node_id"], payload["mount_id"])
                if key[0] and key[1]:
                    latest[key] = payload["code"]
            except (KeyError, TypeError, ValueError):
                continue
        self._blocked_mounts = {key for key, code in latest.items() if code == "BLOCKED_CAPACITY"}

    def probe_path(self, path: str | Path, *, node_id: str,
                   capabilities=(), now: datetime | None = None) -> CapacitySnapshot:
        """Measure the existing target path's actual filesystem, not `/` by assumption."""
        requested = Path(path).expanduser()
        candidate = requested
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        target = candidate.resolve(strict=True)
        if not target.is_dir():
            raise ValueError("capacity target must be an existing directory")
        stats = os.statvfs(target)
        block_size = stats.f_frsize or stats.f_bsize
        total_bytes = int(stats.f_blocks * block_size)
        free_bytes = int(stats.f_bavail * block_size)
        total_inodes = int(stats.f_files)
        free_inodes = int(stats.f_favail)
        used_percent = (100.0 * (total_bytes - max(0, free_bytes)) / total_bytes) if total_bytes else 100.0
        inode_used_percent = (100.0 * (total_inodes - max(0, free_inodes)) / total_inodes) if total_inodes else 100.0
        mount_id = f"dev-{target.stat().st_dev:x}"
        return CapacitySnapshot(
            node_id=node_id, mount_id=mount_id, total_bytes=total_bytes,
            free_bytes=free_bytes, used_percent=used_percent,
            total_inodes=total_inodes, free_inodes=free_inodes,
            inode_used_percent=inode_used_percent, observed_at=_utc(now or datetime.now(timezone.utc)),
            capabilities=frozenset(str(item) for item in capabilities), online=True,
        )

    def evaluate_paths(self, paths, *, node_id: str, capabilities=(),
                       now: datetime | None = None, heavy: bool = True) -> CapacityDecision:
        """Probe every trusted target path and return the strictest result."""
        observed = now or datetime.now(timezone.utc)
        snapshots = []
        for path in paths:
            try:
                snapshots.append(self.probe_path(path, node_id=node_id,
                                                 capabilities=capabilities, now=observed))
            except (OSError, ValueError, TypeError):
                return CapacityDecision("BLOCKED_CAPACITY", node_id, None,
                                        "CAPACITY_PROBE_FAILED", observed, None, None, None)
        if not snapshots:
            return CapacityDecision("BLOCKED_CAPACITY", node_id, None,
                                    "CAPACITY_TARGETS_MISSING", observed, None, None, None)
        decisions = []
        for snapshot in snapshots:
            key = (snapshot.node_id, snapshot.mount_id)
            decision = self.evaluate(snapshot, now=observed, heavy=heavy,
                                     previously_blocked=key in self._blocked_mounts)
            if decision.code == "BLOCKED_CAPACITY":
                self._blocked_mounts.add(key)
            elif decision.code in {"ALLOW", "WARN_CAPACITY"}:
                self._blocked_mounts.discard(key)
            decisions.append(decision)
        priority = {"BLOCKED_CAPACITY": 2, "WARN_CAPACITY": 1, "ALLOW": 0, "UNKNOWN_CAPACITY": 2}
        return max(decisions, key=lambda item: priority.get(item.code, 2))

    def evaluate(self, snapshot: CapacitySnapshot | None, *, now: datetime,
                 heavy: bool, previously_blocked: bool = False) -> CapacityDecision:
        if not heavy:
            return CapacityDecision("UNKNOWN_CAPACITY", snapshot.node_id if snapshot else None,
                                    snapshot.mount_id if snapshot else None,
                                    "capacity gate applies to heavy dispatch only",
                                    snapshot.observed_at if snapshot else None,
                                    snapshot.free_bytes if snapshot else None,
                                    snapshot.used_percent if snapshot else None,
                                    snapshot.inode_used_percent if snapshot else None)
        if snapshot is None:
            return CapacityDecision("BLOCKED_CAPACITY", None, None, "CAPACITY_UNKNOWN", None, None, None, None)
        try:
            observed = _utc(snapshot.observed_at)
            current = _utc(now)
            values_valid = (
                bool(snapshot.node_id and snapshot.mount_id)
                and snapshot.total_bytes > 0
                and 0 <= snapshot.free_bytes <= snapshot.total_bytes
                and 0 <= snapshot.used_percent <= 100
                and abs(snapshot.used_percent - (
                    100.0 * (snapshot.total_bytes - snapshot.free_bytes) / snapshot.total_bytes
                )) <= 1.0
                and snapshot.total_inodes > 0
                and 0 <= snapshot.free_inodes <= snapshot.total_inodes
                and 0 <= snapshot.inode_used_percent <= 100
                and abs(snapshot.inode_used_percent - (
                    100.0 * (snapshot.total_inodes - snapshot.free_inodes) / snapshot.total_inodes
                )) <= 1.0
            )
        except (AttributeError, TypeError, ValueError):
            values_valid = False
            observed = None
            current = _utc(now)
        if not values_valid:
            return self._decision("BLOCKED_CAPACITY", snapshot, "CAPACITY_METRICS_INVALID")
        age = (current - observed).total_seconds()
        if not snapshot.online or age < 0 or age > self.policy.freshness_ttl_seconds:
            reason = "NODE_OFFLINE" if not snapshot.online else "CAPACITY_STALE"
            return self._decision("BLOCKED_CAPACITY", snapshot, reason)

        policy = self.policy
        if previously_blocked:
            is_blocked = (snapshot.used_percent >= policy.clear_used_percent
                          or snapshot.free_bytes < policy.clear_free_bytes
                          or snapshot.inode_used_percent >= policy.clear_inode_used_percent)
            reason = "CAPACITY_HYSTERESIS_HOLD"
        else:
            is_blocked = (snapshot.used_percent >= policy.block_used_percent
                          or snapshot.free_bytes < policy.block_free_bytes
                          or snapshot.inode_used_percent >= policy.block_inode_used_percent)
            reason = "CAPACITY_THRESHOLD_EXCEEDED"
        if is_blocked:
            return self._decision("BLOCKED_CAPACITY", snapshot, reason)
        if (snapshot.used_percent >= policy.warn_used_percent
                or snapshot.inode_used_percent >= policy.warn_inode_used_percent):
            return self._decision("WARN_CAPACITY", snapshot, "CAPACITY_WARNING_BAND", rank_penalty=1)
        return self._decision("ALLOW", snapshot, "CAPACITY_HEALTHY")

    def select_node(self, snapshots: list[CapacitySnapshot], *, now: datetime,
                    required_capabilities=(), pinned_node: str | None = None,
                    heavy: bool = True, previously_blocked_nodes=frozenset()) -> CapacityDecision:
        by_id = {item.node_id: item for item in snapshots}
        required = frozenset(required_capabilities)
        if pinned_node:
            target = by_id.get(pinned_node)
            if target is None:
                return CapacityDecision("BLOCKED_CAPACITY", pinned_node, None,
                                        "PINNED_NODE_CAPACITY_UNKNOWN", None, None, None, None)
            if not required.issubset(target.capabilities):
                return self._decision("BLOCKED_CAPACITY", target, "PINNED_NODE_INCOMPATIBLE")
            result = self.evaluate(target, now=now, heavy=heavy,
                                   previously_blocked=pinned_node in previously_blocked_nodes)
            return result

        preferences = (self.policy.preference_docker if "docker" in required
                       else self.policy.preference_non_docker)
        candidates = []
        for rank, node_id in enumerate(preferences):
            target = by_id.get(node_id)
            if target is None or not required.issubset(target.capabilities):
                continue
            result = self.evaluate(target, now=now, heavy=heavy,
                                   previously_blocked=node_id in previously_blocked_nodes)
            if result.allowed:
                # A warning band demotes a node even when it has higher
                # nominal preference; healthy compatible nodes win.
                candidates.append((result.rank_penalty, rank, result))
        if candidates:
            return min(candidates, key=lambda item: (item[0], item[1]))[2]
        if not by_id:
            return CapacityDecision("BLOCKED_CAPACITY", None, None, "NO_CAPACITY_OBSERVATIONS", None, None, None, None)
        return CapacityDecision("BLOCKED_CAPACITY", None, None,
                                "NO_FRESH_COMPATIBLE_NODE", None, None, None, None)

    @staticmethod
    def _decision(code: str, snapshot: CapacitySnapshot, reason: str,
                  *, rank_penalty: int = 0) -> CapacityDecision:
        return CapacityDecision(code, snapshot.node_id, snapshot.mount_id, reason,
                                snapshot.observed_at, snapshot.free_bytes,
                                snapshot.used_percent, snapshot.inode_used_percent,
                                rank_penalty=rank_penalty)
