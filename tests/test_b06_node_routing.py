from __future__ import annotations

import pytest

from app.services.node_router import NodeRoutingError, select_node


def node(name, **overrides):
    value = {"id": name, "status": "online", "capacity_status": "healthy", "free_bytes": 10_000, "tools": ["codex"]}
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "bad",
    [
        node("offline", status="offline"),
        node("low-space", free_bytes=99),
        node("missing-tool", tools=["python"]),
    ],
)
def test_routing_rejects_unusable_enrolled_nodes(bad):
    with pytest.raises(NodeRoutingError, match="no eligible node"):
        select_node([bad], required_tool="codex", min_free_bytes=100)


def test_routing_rejects_unknown_requested_node_without_fallback():
    with pytest.raises(NodeRoutingError, match="unknown node"):
        select_node([node("healthy")], required_tool="codex", requested_node="missing")


def test_routing_selects_healthy_alternate_deterministically():
    chosen = select_node(
        [node("zeta", status="offline"), node("beta"), node("alpha", capacity_status="full")],
        required_tool="codex",
        min_free_bytes=100,
    )
    assert chosen["id"] == "beta"
