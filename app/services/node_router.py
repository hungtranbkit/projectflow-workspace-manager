"""Fail-closed selection of an already enrolled execution node.

This module only makes the admission decision.  It never creates a session or
changes node state; the normal launcher remains responsible for the subsequent
authenticated dispatch.  Keeping the decision pure makes offline, capacity,
and capability failures deterministic and auditable.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone


class NodeRoutingError(ValueError):
    """No enrolled node satisfied the requested execution contract."""


def select_node(
    nodes: Iterable[Mapping[str, object]],
    *,
    required_tool: str,
    min_free_bytes: int = 0,
    requested_node: str | None = None,
    max_evidence_age_seconds: int = 300,
) -> Mapping[str, object]:
    """Return the deterministic healthy node for a bounded task.

    ``nodes`` is the controller's current enrolled-node snapshot.  A node is
    eligible only when it is online, reports healthy capacity, has the pinned
    tool, and meets the disk floor.  A requested node is an exact constraint;
    an unknown or unhealthy request never silently falls back to a different
    node.  Without a request, the lexicographically first eligible node is the
    stable alternate-node choice.
    """
    tool = str(required_tool or "").strip().lower()
    if not tool:
        raise NodeRoutingError("required tool is missing")
    if min_free_bytes < 0 or max_evidence_age_seconds <= 0:
        raise NodeRoutingError("minimum free space must be non-negative")
    candidates: list[Mapping[str, object]] = []
    seen = False
    for node in nodes:
        node_id = str(node.get("id") or node.get("name") or "").strip()
        if requested_node and node_id == requested_node:
            seen = True
        if not node_id or (requested_node and node_id != requested_node):
            continue
        if node.get("draining") or str(node.get("status", "")).lower() != "online":
            continue
        if str(node.get("capacity_status", "")).lower() not in {"healthy", "ready"}:
            continue
        observed = node.get("capacity_observed_at")
        if not observed:
            continue
        try:
            stamp = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if not 0 <= age <= max_evidence_age_seconds:
                continue
        except (TypeError, ValueError):
            continue
        free_bytes = node.get("free_bytes")
        if type(free_bytes) is not int or free_bytes < min_free_bytes:
            continue
        tools = node.get("tools", node.get("capabilities", ()))
        if isinstance(tools, Mapping):
            tools = [name for name, evidence in tools.items() if evidence is True or str(evidence).lower() in {"ready", "verified", "healthy"}]
        if tool not in {str(item).strip().lower() for item in (tools or ())}:
            continue
        candidates.append(node)
    if requested_node and not seen:
        raise NodeRoutingError(f"unknown node: {requested_node}")
    if not candidates:
        target = f" {requested_node}" if requested_node else ""
        raise NodeRoutingError(f"no eligible node{target}: offline, capacity, disk, or tool gate")
    return sorted(candidates, key=lambda row: str(row.get("id") or row.get("name")))[0]
