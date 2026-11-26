"""Fresh filesystem capacity checks used before resource-heavy dispatch.

The service is pure apart from probe_path(); callers own trusted target paths
and supply current node capabilities. It never starts work or deletes data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
from pathlib import PurePosixPath

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
    used_bytes: int
    free_bytes: int
    used_percent: float
    total_inodes: int
    used_inodes: int
    free_inodes: int
    inode_used_percent: float
    observed_at: datetime
    capabilities: frozenset[str] = field(default_factory=frozenset)
    online: bool = True


@dataclass(frozen=True)
class CapacityLease:
    resource_key: str
    owner_id: str
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class CapacityMountIdentity:
    boot_id_sha256: str
    mount_namespace: str
    kernel_mount_id: str
    st_dev: str
    directory_inode: int
    mountpoint: str
    mount_root: str
    fs_type: str
    major_minor: str

    def filesystem_id(self) -> str:
        fields = (self.boot_id_sha256, self.mount_namespace, self.kernel_mount_id, self.st_dev, self.mountpoint, self.mount_root, self.fs_type, self.major_minor)
        return hashlib.sha256(json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()

    def stable_id(self) -> str:
        fields = (self.boot_id_sha256, self.mount_namespace, self.kernel_mount_id,
                  self.st_dev, self.directory_inode, self.mountpoint,
                  self.mount_root, self.fs_type, self.major_minor)
        encoded = json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CapacityTargetRequest:
    role: str
    path: str
    expected_mount: CapacityMountIdentity


@dataclass(frozen=True)
class RawFilesystemCounters:
    f_frsize: int
    f_blocks: int
    f_bfree: int
    f_bavail: int
    f_files: int
    f_ffree: int
    f_favail: int


@dataclass(frozen=True)
class CapacityTargetReceipt:
    role: str
    requested_path: str
    canonical_existing_ancestor: str
    relative_suffix: str
    mount: CapacityMountIdentity
    counters: RawFilesystemCounters


@dataclass(frozen=True)
class CapacityCapabilityEvidence:
    name: str
    evidence_level: str
    code: str
    observed_at: datetime


@dataclass(frozen=True)
class CapacityAdmissionRequest:
    schema_version: int
    request_key: str
    task_id: int
    project_id: str
    node_id: str
    identity_revision: str
    workspace: str
    workspace_revision: str
    lease: CapacityLease
    targets: tuple[CapacityTargetRequest, ...]
    required_capabilities: frozenset[str]
    requested_headroom_bytes: int = 0

    def fingerprint(self) -> str:
        def canonical(value):
            if isinstance(value, datetime):
                return _utc(value).isoformat()
            if hasattr(value, "__dataclass_fields__"):
                return {name: canonical(getattr(value, name)) for name in value.__dataclass_fields__}
            if isinstance(value, (set, frozenset)):
                return sorted(canonical(item) for item in value)
            if isinstance(value, tuple):
                return [canonical(item) for item in value]
            return value

        body = json.dumps(canonical(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class CapacityReceipt:
    schema_version: int
    receipt_id: str
    request_key: str
    request_sha256: str
    task_id: int
    project_id: str
    node_id: str
    identity_revision: str
    workspace: str
    workspace_revision: str
    lease: CapacityLease
    controller_received_at: datetime
    observed_at: datetime
    expires_at: datetime
    targets: tuple[CapacityTargetReceipt, ...]
    capabilities: tuple[CapacityCapabilityEvidence, ...]
    online: bool


@dataclass(frozen=True)
class VerifiedCapacityReceipt:
    """Typed verifier output. `issuer` must be the gate's injected verifier."""
    receipt: CapacityReceipt
    issuer: object = field(repr=False, compare=False)


class CapacityReceiptVerifier:
    """Boundary implemented by the owning authenticated node adapter.

    Production has no verifier wired yet, so remote admission remains
    fail-closed. Tests inject a typed fixture verifier; no JSON/hash alone
    can establish trusted provenance.
    """
    def verify(self, receipt: CapacityReceipt) -> VerifiedCapacityReceipt:
        raise NotImplementedError


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
    total_bytes: int | None = None
    used_bytes: int | None = None
    total_inodes: int | None = None
    used_inodes: int | None = None

    filesystem_id: str | None = None
    mount_decisions: tuple = ()

    @property
    def allowed(self) -> bool:
        return self.code in {"ALLOW", "WARN_CAPACITY"}

    def audit_fields(self) -> dict:
        return {
            "code": self.code, "node_id": self.node_id, "mount_id": self.mount_id,
            "reason": self.reason, "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "total_bytes": self.total_bytes, "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes, "used_percent": self.used_percent,
            "total_inodes": self.total_inodes, "used_inodes": self.used_inodes,
            "inode_used_percent": self.inode_used_percent,
            "rank_penalty": self.rank_penalty, "rerouted": self.rerouted,
            "filesystem_id": self.filesystem_id, "mount_decisions": list(self.mount_decisions),
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
    def __init__(self, policy: CapacityPolicy | None = None, *, receipt_verifier=None):
        self.policy = policy or CapacityPolicy()
        self._receipt_verifier = receipt_verifier
        self._blocked_mounts: set[tuple[str, str]] = set()

    @staticmethod
    def _receipt_block(reason: str, *, node_id=None, mount_id=None, observed_at=None):
        return CapacityDecision("BLOCKED_CAPACITY", node_id, mount_id, reason,
                                observed_at, None, None, None)

    @staticmethod
    def _valid_mount_identity(mount: CapacityMountIdentity) -> bool:
        return (
            isinstance(mount, CapacityMountIdentity)
            and all(isinstance(value, str) and value.strip() for value in (
                mount.boot_id_sha256, mount.mount_namespace, mount.kernel_mount_id,
                mount.st_dev, mount.mountpoint, mount.mount_root, mount.fs_type,
                mount.major_minor,
            ))
            and mount.mountpoint.startswith("/") and mount.mount_root.startswith("/")
            and len(mount.boot_id_sha256) == 64
            and all(char in "0123456789abcdef" for char in mount.boot_id_sha256)
            and type(mount.directory_inode) is int and mount.directory_inode > 0
        )

    def evaluate_receipt(self, request: CapacityAdmissionRequest, receipt: CapacityReceipt,
                         *, now: datetime, heavy: bool) -> CapacityDecision:
        """Validate an authenticated, request-bound target receipt.

        No remote producer is configured in ProjectFlow yet. The method is
        deliberately inert unless the existing owner injects a verifier; raw
        request JSON, self-declared hashes and caller-selected trust flags are
        never an authorization source.
        """
        try:
            current = _utc(now)
        except (TypeError, ValueError):
            return self._receipt_block("CAPACITY_TIME_INVALID")
        if not heavy:
            return self._receipt_block("CAPACITY_GATE_HEAVY_ONLY",
                                       node_id=getattr(request, "node_id", None))
        if self._receipt_verifier is None:
            return self._receipt_block("CAPACITY_RECEIPT_VERIFIER_UNAVAILABLE",
                                       node_id=getattr(request, "node_id", None))
        if not isinstance(request, CapacityAdmissionRequest) or not isinstance(receipt, CapacityReceipt):
            return self._receipt_block("CAPACITY_RECEIPT_TYPE_INVALID")
        try:
            verified = self._receipt_verifier.verify(receipt)
        except Exception:
            return self._receipt_block("CAPACITY_RECEIPT_UNTRUSTED", node_id=request.node_id)
        if (not isinstance(verified, VerifiedCapacityReceipt)
                or verified.issuer is not self._receipt_verifier
                or verified.receipt is not receipt):
            return self._receipt_block("CAPACITY_RECEIPT_UNTRUSTED", node_id=request.node_id)

        try:
            lease_start = _utc(request.lease.acquired_at)
            lease_end = _utc(request.lease.expires_at)
            received = _utc(receipt.controller_received_at)
            observed = _utc(receipt.observed_at)
            receipt_end = _utc(receipt.expires_at)
            cap_ttl = timedelta(seconds=self.policy.freshness_ttl_seconds)
        except (AttributeError, TypeError, ValueError):
            return self._receipt_block("CAPACITY_RECEIPT_TIMESTAMPS_INVALID",
                                       node_id=request.node_id)

        if (type(request.schema_version) is not int or request.schema_version != 1
                or not isinstance(request.request_key, str) or not request.request_key
                or type(request.task_id) is not int or request.task_id <= 0
                or any(not isinstance(value, str) or not value for value in (
                    request.project_id, request.node_id, request.identity_revision,
                    request.workspace, request.workspace_revision,
                ))
                or type(request.requested_headroom_bytes) is not int
                or request.requested_headroom_bytes < 0
                or not isinstance(request.lease, CapacityLease)
                or not isinstance(request.lease.resource_key, str) or not request.lease.resource_key
                or not isinstance(request.lease.owner_id, str) or not request.lease.owner_id
                or not isinstance(request.targets, tuple) or not request.targets or len(request.targets) > 16
                or any(not isinstance(target, CapacityTargetRequest) for target in request.targets)
                or any(not isinstance(target.role, str) or not target.role for target in request.targets)
                or len({target.role for target in request.targets if isinstance(target, CapacityTargetRequest)}) != len(request.targets)
                or any(not isinstance(target, CapacityTargetRequest) or not target.role or not target.path
                       or not isinstance(target.role, str) or not isinstance(target.path, str)
                       or not target.path.startswith("/") or not self._valid_mount_identity(target.expected_mount)
                       for target in request.targets)
                or not isinstance(request.required_capabilities, frozenset)
                or any(not isinstance(name, str) or not name for name in request.required_capabilities)):
            return self._receipt_block("CAPACITY_REQUEST_INVALID", node_id=request.node_id)

        if (lease_end <= lease_start or current < lease_start or current >= lease_end
                or observed < lease_start):
            return self._receipt_block("CAPACITY_LEASE_INVALID", node_id=request.node_id,
                                       observed_at=observed)

        if (type(receipt.schema_version) is not int or receipt.schema_version != 1
                or not isinstance(receipt.receipt_id, str) or not receipt.receipt_id):
            return self._receipt_block("CAPACITY_RECEIPT_INVALID", node_id=request.node_id,
                                       observed_at=observed)
        if receipt.online is not True:
            reason = "NODE_OFFLINE"
            return self._receipt_block(reason, node_id=request.node_id,
                                       observed_at=observed)
        if current < observed or current < received:
            return self._receipt_block("CAPACITY_FUTURE", node_id=request.node_id,
                                       observed_at=observed)
        if received < observed:
            return self._receipt_block("CAPACITY_RECEIPT_TIMESTAMPS_INVALID",
                                       node_id=request.node_id, observed_at=observed)
        if current - observed > cap_ttl:
            return self._receipt_block("CAPACITY_STALE", node_id=request.node_id,
                                       observed_at=observed)
        if receipt_end <= current:
            return self._receipt_block("CAPACITY_RECEIPT_EXPIRED", node_id=request.node_id,
                                       observed_at=observed)
        if receipt_end > min(lease_end, observed + cap_ttl):
            return self._receipt_block("CAPACITY_RECEIPT_EXPIRY_INVALID",
                                       node_id=request.node_id, observed_at=observed)

        if (receipt.request_key != request.request_key or receipt.task_id != request.task_id
                or receipt.project_id != request.project_id or receipt.workspace != request.workspace
                or receipt.workspace_revision != request.workspace_revision):
            return self._receipt_block("CAPACITY_REQUEST_MISMATCH", node_id=request.node_id,
                                       observed_at=observed)
        if receipt.lease != request.lease:
            return self._receipt_block("CAPACITY_LEASE_MISMATCH", node_id=request.node_id,
                                       observed_at=observed)
        if receipt.node_id != request.node_id or receipt.identity_revision != request.identity_revision:
            return self._receipt_block("CAPACITY_NODE_IDENTITY_MISMATCH", node_id=request.node_id,
                                       observed_at=observed)
        try:
            expected_hash = request.fingerprint()
        except (TypeError, ValueError):
            return self._receipt_block("CAPACITY_REQUEST_INVALID", node_id=request.node_id)
        if (not isinstance(receipt.request_sha256, str)
                or len(receipt.request_sha256) != 64
                or any(c not in "0123456789abcdef" for c in receipt.request_sha256)
                or not hmac.compare_digest(receipt.request_sha256, expected_hash)):
            return self._receipt_block("CAPACITY_REQUEST_HASH_MISMATCH", node_id=request.node_id,
                                       observed_at=observed)

        targets = receipt.targets
        if (not isinstance(targets, tuple) or not targets or len(targets) > 16
                or any(not isinstance(item, CapacityTargetReceipt) for item in targets)
                or any(not isinstance(item.role, str) or not item.role for item in targets)
                or len({item.role for item in targets if isinstance(item, CapacityTargetReceipt)}) != len(targets)):
            return self._receipt_block("CAPACITY_TARGETS_MISMATCH", node_id=request.node_id,
                                       observed_at=observed)
        requested_by_role = {item.role: item for item in request.targets}
        received_by_role = {item.role: item for item in targets if isinstance(item, CapacityTargetReceipt)}
        if requested_by_role.keys() != received_by_role.keys():
            return self._receipt_block("CAPACITY_TARGETS_MISMATCH", node_id=request.node_id,
                                       observed_at=observed)

        capabilities = receipt.capabilities
        if (not isinstance(capabilities, tuple)
                or any(not isinstance(item, CapacityCapabilityEvidence) for item in capabilities)
                or any(not isinstance(item.name, str) or not item.name for item in capabilities)
                or len({item.name for item in capabilities
                        if isinstance(item, CapacityCapabilityEvidence)}) != len(capabilities)):
            return self._receipt_block("CAPABILITY_MISMATCH", node_id=request.node_id,
                                       observed_at=observed)
        capability_by_name = {item.name: item for item in capabilities
                              if isinstance(item, CapacityCapabilityEvidence)}
        for name in request.required_capabilities:
            evidence = capability_by_name.get(name)
            minimum_level = "runtime" if name == "docker" else "binary"
            allowed_levels = {"binary": {"binary", "runtime", "authenticated"},
                              "runtime": {"runtime", "authenticated"}}
            if (evidence is None or evidence.code != "SUPPORTED"
                    or evidence.evidence_level not in allowed_levels[minimum_level]):
                return self._receipt_block("CAPABILITY_MISMATCH", node_id=request.node_id,
                                           observed_at=observed)
            try:
                cap_observed = _utc(evidence.observed_at)
            except (TypeError, ValueError):
                return self._receipt_block("CAPABILITY_MISMATCH", node_id=request.node_id,
                                           observed_at=observed)
            if cap_observed > current or current - cap_observed > cap_ttl:
                return self._receipt_block("CAPABILITY_MISMATCH", node_id=request.node_id,
                                           observed_at=cap_observed)

        decisions = []
        for role, target in requested_by_role.items():
            evidence = received_by_role[role]
            if (not isinstance(evidence.requested_path, str)
                    or not isinstance(evidence.canonical_existing_ancestor, str)
                    or not isinstance(evidence.relative_suffix, str)
                    or evidence.requested_path != target.path):
                return self._receipt_block("CAPACITY_TARGET_MISMATCH", node_id=request.node_id,
                                           observed_at=observed)
            if (not self._valid_mount_identity(evidence.mount)
                    or evidence.mount != target.expected_mount):
                return self._receipt_block("CAPACITY_MOUNT_MISMATCH", node_id=request.node_id,
                                           mount_id=evidence.mount.stable_id() if self._valid_mount_identity(evidence.mount) else None,
                                           observed_at=observed)
            ancestor = PurePosixPath(evidence.canonical_existing_ancestor)
            suffix = PurePosixPath(evidence.relative_suffix)
            if (not ancestor.is_absolute() or suffix.is_absolute() or not evidence.relative_suffix
                    or ".." in ancestor.parts or ".." in suffix.parts
                    or str(ancestor.joinpath(suffix)) != evidence.requested_path):
                return self._receipt_block("CAPACITY_TARGET_MISMATCH", node_id=request.node_id,
                                           observed_at=observed)
            counters = evidence.counters
            if not isinstance(counters, RawFilesystemCounters):
                return self._receipt_block("CAPACITY_METRICS_INVALID", node_id=request.node_id,
                                           mount_id=evidence.mount.stable_id(), observed_at=observed)
            raw_values = (counters.f_frsize, counters.f_blocks, counters.f_bfree,
                          counters.f_bavail, counters.f_files, counters.f_ffree,
                          counters.f_favail)
            if (any(type(value) is not int for value in raw_values)
                    or counters.f_frsize <= 0 or counters.f_blocks <= 0
                    or counters.f_bfree < 0 or counters.f_bfree > counters.f_blocks
                    or counters.f_bavail < 0 or counters.f_bavail > counters.f_bfree
                    or counters.f_files <= 0 or counters.f_ffree < 0
                    or counters.f_ffree > counters.f_files or counters.f_favail < 0
                    or counters.f_favail > counters.f_ffree):
                return self._receipt_block("CAPACITY_METRICS_INVALID", node_id=request.node_id,
                                           mount_id=evidence.mount.stable_id(), observed_at=observed)
            used_bytes = (counters.f_blocks - counters.f_bfree) * counters.f_frsize
            free_bytes = counters.f_bavail * counters.f_frsize
            used_inodes = counters.f_files - counters.f_ffree
            free_inodes = counters.f_favail
            if used_bytes + free_bytes <= 0 or used_inodes + free_inodes <= 0:
                return self._receipt_block("CAPACITY_METRICS_INVALID", node_id=request.node_id,
                                           mount_id=evidence.mount.stable_id(), observed_at=observed)
            snapshot = CapacitySnapshot(
                node_id=request.node_id,
                mount_id=evidence.mount.stable_id(),
                total_bytes=counters.f_blocks * counters.f_frsize,
                used_bytes=used_bytes, free_bytes=free_bytes,
                used_percent=100.0 * used_bytes / (used_bytes + free_bytes),
                total_inodes=counters.f_files, used_inodes=used_inodes,
                free_inodes=free_inodes,
                inode_used_percent=100.0 * used_inodes / (used_inodes + free_inodes),
                observed_at=observed, capabilities=request.required_capabilities, online=True,
            )
            physical_id = evidence.mount.filesystem_id()
            key = (snapshot.node_id, physical_id)
            decision = self.evaluate(
                snapshot, now=current, heavy=True,
                previously_blocked=key in self._blocked_mounts,
            )
            if decision.allowed and free_bytes < max(self.policy.block_free_bytes,
                                                      request.requested_headroom_bytes):
                decision = self._decision("BLOCKED_CAPACITY", snapshot,
                                          "REQUESTED_HEADROOM_UNMET")
            decisions.append(__import__("dataclasses").replace(decision, filesystem_id=physical_id))

        priority = {"BLOCKED_CAPACITY": 2, "UNKNOWN_CAPACITY": 2,
                    "WARN_CAPACITY": 1, "ALLOW": 0}
        by_filesystem = {}
        for item in decisions:
            key = (item.node_id, item.filesystem_id)
            if key not in by_filesystem or priority.get(item.code, 2) > priority.get(by_filesystem[key].code, 2):
                by_filesystem[key] = item
        for key, item in by_filesystem.items():
            if item.code == "BLOCKED_CAPACITY":
                self._blocked_mounts.add(key)
            else:
                self._blocked_mounts.discard(key)
        result = max(decisions, key=lambda item: priority.get(item.code, 2))
        return __import__("dataclasses").replace(result, mount_decisions=tuple({"node_id": item.node_id, "filesystem_id": item.filesystem_id, "code": item.code} for item in by_filesystem.values()))

    def restore_hysteresis(self, events) -> None:
        """Restore mount state from append-only capacity audit events."""
        latest = {}
        for event in events:
            try:
                payload = json.loads(event["details"])
                for item in payload.get("mount_decisions") or [payload]:
                    key = (item["node_id"], item.get("filesystem_id") or item.get("mount_id"))
                    if key[0] and key[1]:
                        latest[key] = item["code"]
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
        if (block_size <= 0 or stats.f_blocks <= 0 or stats.f_bfree < 0
                or stats.f_bavail < 0 or stats.f_bfree > stats.f_blocks
                or stats.f_bavail > stats.f_bfree):
            raise ValueError("filesystem block metrics are invalid")
        if (stats.f_files <= 0 or stats.f_ffree < 0 or stats.f_favail < 0
                or stats.f_ffree > stats.f_files or stats.f_favail > stats.f_ffree):
            raise ValueError("filesystem inode metrics are invalid")
        total_bytes = int(stats.f_blocks * block_size)
        used_bytes = int((stats.f_blocks - stats.f_bfree) * block_size)
        free_bytes = int(stats.f_bavail * block_size)
        total_inodes = int(stats.f_files)
        used_inodes = int(stats.f_files - stats.f_ffree)
        free_inodes = int(stats.f_favail)
        used_and_available = used_bytes + free_bytes
        if used_and_available <= 0:
            raise ValueError("filesystem block metrics are invalid")
        # Match df's Use%: used / (used + available). f_bavail excludes
        # reserved blocks, while f_bfree includes them; treating f_bavail as
        # all free blocks overstates usage on filesystems with reservations.
        used_percent = 100.0 * used_bytes / used_and_available
        inodes_used_and_available = used_inodes + free_inodes
        if inodes_used_and_available <= 0:
            raise ValueError("filesystem inode metrics are invalid")
        inode_used_percent = 100.0 * used_inodes / inodes_used_and_available
        mount_id = f"dev-{target.stat().st_dev:x}"
        return CapacitySnapshot(
            node_id=node_id, mount_id=mount_id, total_bytes=total_bytes,
            used_bytes=used_bytes, free_bytes=free_bytes, used_percent=used_percent,
            total_inodes=total_inodes, used_inodes=used_inodes, free_inodes=free_inodes,
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
                and 0 <= snapshot.used_bytes <= snapshot.total_bytes
                and 0 <= snapshot.free_bytes <= snapshot.total_bytes - snapshot.used_bytes
                and snapshot.used_bytes + snapshot.free_bytes > 0
                and 0 <= snapshot.used_percent <= 100
                and abs(snapshot.used_percent - (
                    100.0 * snapshot.used_bytes / (snapshot.used_bytes + snapshot.free_bytes)
                )) <= 1.0
                and snapshot.total_inodes > 0
                and 0 <= snapshot.used_inodes <= snapshot.total_inodes
                and 0 <= snapshot.free_inodes <= snapshot.total_inodes - snapshot.used_inodes
                and snapshot.used_inodes + snapshot.free_inodes > 0
                and 0 <= snapshot.inode_used_percent <= 100
                and abs(snapshot.inode_used_percent - (
                    100.0 * snapshot.used_inodes / (snapshot.used_inodes + snapshot.free_inodes)
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
                                rank_penalty=rank_penalty,
                                total_bytes=snapshot.total_bytes, used_bytes=snapshot.used_bytes,
                                total_inodes=snapshot.total_inodes, used_inodes=snapshot.used_inodes)
