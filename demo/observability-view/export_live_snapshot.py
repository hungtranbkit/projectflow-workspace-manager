#!/usr/bin/env python3
"""Public, aggregate-only Terminal MCP snapshot. No prompts, IDs or logs.

Both SQLite inputs are opened read-only. The output is intended to be
published on a public static page; never add raw node/session columns.
An ACTIVE session is an open session, NOT verified running code.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def _instant(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(timezone.utc)
    except ValueError:
        return None


def _recent(value: str | None, now: datetime, seconds: int) -> bool:
    instant = _instant(value)
    if instant is None:
        return False
    age = (now - instant).total_seconds()
    return 0 <= age <= seconds


def _read(path: Path, query: str) -> list[sqlite3.Row]:
    # Enforces no database mutation; never use a plain writable connect.
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        return list(con.execute(query))
    finally:
        con.close()


def snapshot(nodes_db: Path, sessions_db: Path, *,
             now: datetime | None = None, fresh_seconds: int = 300) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must have timezone")
    now = now.astimezone(timezone.utc)
    if not 60 <= fresh_seconds <= 3600:
        raise ValueError("fresh_seconds outside bounded range")

    # Explicit allowlist: do not select hostname, endpoint, auth_token_ref,
    # session_name, launch_command, cwd, notes, prompts, tags or logs.
    nodes = _read(nodes_db,
                  "SELECT id, last_known_status, last_heartbeat_at FROM nodes ORDER BY id")
    sessions = _read(
        sessions_db,
        "SELECT node_id, last_seen_at, last_activity_at FROM session_records "
        "WHERE status='ACTIVE' AND deleted_at IS NULL"
    )

    open_per_node = Counter(row["node_id"] for row in sessions)
    fresh_per_node = Counter(
        row["node_id"] for row in sessions
        if _recent(row["last_activity_at"], now, fresh_seconds)
    )
    visible_nodes = []
    for index, node in enumerate(nodes, start=1):
        raw_status = (node["last_known_status"] or "").lower()
        heartbeat_fresh = _recent(node["last_heartbeat_at"], now, fresh_seconds)
        if raw_status == "online" and heartbeat_fresh:
            status = "online"
        elif raw_status == "offline":
            status = "offline"
        else:
            status = "unknown"
        visible_nodes.append({
            "label": "Node %02d" % index,  # Stable, anonymous alias.
            "status": status,
            "open_sessions": open_per_node[node["id"]],
            "recent_activity_signals": fresh_per_node[node["id"]],
        })
    counts = Counter(x["status"] for x in visible_nodes)
    return {
        "schema_version": 1,
        "kind": "terminal-mcp-public-aggregate",
        "observed_at": now.isoformat(timespec="seconds"),
        "expected_refresh_seconds": 300,
        "fresh_window_seconds": fresh_seconds,
        "nodes": {
            "total": len(visible_nodes),
            "online": counts["online"],
            "offline": counts["offline"],
            "unknown": counts["unknown"],
            "items": visible_nodes,
        },
        "sessions": {
            "open": len(sessions),
            "recent_activity_signals": sum(fresh_per_node.values()),
            "running_verified": None,
        },
        "acceptance": {"percent": None, "ttl_percent": None},
        "source": "Terminal MCP local read-only registry",
        "notes": [
            "Phiên ACTIVE chỉ là phiên còn mở, không chứng minh agent đang code.",
            "Tín hiệu phiên không phải bằng chứng tiến độ hoặc nghiệm thu.",
            "Không công bố địa chỉ, tên task, prompt, token, path hay log thô.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes-db", type=Path, required=True)
    ap.add_argument("--sessions-db", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    result = snapshot(args.nodes_db, args.sessions_db)
    path = args.output
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stage under the same directory, then replace atomically.
    fd, staging = tempfile.mkstemp(prefix=".live-snapshot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(staging, 0o600)
        os.replace(staging, path)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)
    print("SNAPSHOT_OK nodes=%d online=%d open_sessions=%d fresh_signals=%d" % (
        result["nodes"]["total"], result["nodes"]["online"],
        result["sessions"]["open"], result["sessions"]["recent_activity_signals"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
