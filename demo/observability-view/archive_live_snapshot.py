#!/usr/bin/env python3
"""Project the public fleet snapshot into the aggregate-only archive.

Never refresh an archive from controller-local ACTIVE registry rows. Preserve
the source observation timestamp, require complete reachable-node coverage,
and publish no per-session details even if the live feed contains them.
"""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import os
import re
import sys
import tempfile

from export_live_snapshot import PUBLIC_NODE_NAMES

MAX_BYTES = 1024 * 1024
MAX_AGE = 120


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def count(value):
    return type(value) is int and 0 <= value <= 100000


def label(value):
    return isinstance(value, str) and (
        value in PUBLIC_NODE_NAMES or re.fullmatch(r"Node \d{2}", value) is not None
    )


def project(data, now=None):
    now = now or datetime.now(timezone.utc)
    require(isinstance(data, dict) and data.get("schema_version") == 2
            and data.get("kind") == "terminal-mcp-public-aggregate",
            "expected live schema 2")
    observed_at = data.get("observed_at")
    require(isinstance(observed_at, str), "missing observation timestamp")
    instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    require(instant.tzinfo is not None and -30 <= (now - instant).total_seconds() <= MAX_AGE,
            "stale or future observation")
    nodes, sessions = data.get("nodes"), data.get("sessions")
    require(isinstance(nodes, dict) and isinstance(sessions, dict), "missing aggregates")
    require(sessions.get("observation_source") == "terminal_mcp_authenticated_fleet",
            "source is not authenticated fleet inventory")
    coverage = sessions.get("coverage")
    require(isinstance(coverage, dict), "missing fleet coverage")
    covered, unreachable = coverage.get("covered_nodes"), coverage.get("unreachable_nodes")
    require(isinstance(covered, list) and bool(covered) and isinstance(unreachable, list)
            and len(covered) <= 25 and len(unreachable) <= 25
            and all(label(x) for x in covered + unreachable), "invalid fleet coverage")
    require(len(set(covered)) == len(covered) and len(set(unreachable)) == len(unreachable)
            and not set(covered).intersection(unreachable), "conflicting fleet coverage")
    items = nodes.get("items")
    require(isinstance(items, list) and 0 < len(items) <= 25, "invalid node list")
    safe = []
    names = set()
    raw_statuses = Counter()
    for item in items:
        require(isinstance(item, dict), "invalid node")
        name, status = item.get("label"), item.get("status")
        opened, signals = item.get("open_sessions"), item.get("recent_activity_signals")
        require(label(name) and name not in names, "invalid or duplicate public node label")
        require(status in ("online", "offline", "unknown", "degraded"), "invalid node status")
        require(count(opened) and count(signals) and signals <= opened, "invalid node counts")
        if status == "offline":
            require(name not in covered and opened == 0, "offline node has live sessions")
        else:
            require(name in covered, "incomplete reachable-node coverage")
        names.add(name)
        raw_statuses[status] += 1
        # Schema 1 cannot express degraded; report it conservatively as unknown.
        safe.append({"label": name, "status": "unknown" if status == "degraded" else status,
                     "open_sessions": opened, "recent_activity_signals": signals})
    require(set(covered + unreachable) <= names, "coverage does not match public nodes")
    require(count(nodes.get("total")) and nodes["total"] == len(safe)
            and all(count(nodes.get(k)) and nodes[k] == raw_statuses[k]
                    for k in ("online", "offline", "unknown", "degraded")), "node totals disagree")
    opened = sum(x["open_sessions"] for x in safe)
    signals = sum(x["recent_activity_signals"] for x in safe)
    require(count(opened) and count(signals) and count(sessions.get("open"))
            and count(sessions.get("recent_activity_signals"))
            and sessions["open"] == opened and sessions["recent_activity_signals"] == signals,
            "fleet and per-node session totals disagree")
    window = data.get("fresh_window_seconds")
    require(type(window) is int and 60 <= window <= 3600, "invalid activity window")
    statuses = Counter(x["status"] for x in safe)
    return {
        "schema_version": 1,
        "kind": "terminal-mcp-public-aggregate",
        "observed_at": observed_at,
        "expected_refresh_seconds": 300,
        "fresh_window_seconds": window,
        "nodes": {"total": len(safe), "online": statuses["online"],
                  "offline": statuses["offline"], "unknown": statuses["unknown"], "items": safe},
        "sessions": {"open": opened, "recent_activity_signals": signals, "running_verified": None},
        "acceptance": {"percent": None, "ttl_percent": None},
        "source": "Terminal MCP authenticated fleet snapshot (archived aggregate)",
        "notes": [
            "Bản lưu từ cùng nguồn kiểm kê LIVE; không phải dữ liệu realtime.",
            "Phiên mở và tín hiệu hoạt động không chứng minh agent đang code.",
            "Không lưu chi tiết phiên, nhãn task, branch, địa chỉ, prompt hay log.",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, "snapshot exceeds size limit")
        result = project(json.loads(raw))
    except (ValueError, TypeError, OverflowError):
        # Do not echo untrusted source values into operator logs.
        print("SNAPSHOT_REJECTED: invalid, stale or incomplete fleet snapshot", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=".live-archive-", dir=args.output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(staging, args.output)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)
    print("SNAPSHOT_OK nodes=%d open_sessions=%d" %
          (result["nodes"]["total"], result["sessions"]["open"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
