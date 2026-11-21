#!/usr/bin/env python3
"""Read-only live Terminal MCP aggregate via localhost HTTP + SSE.

Public routes are limited to /projectflow-live/{health,snapshot,events}.
Only owner-approved registered node names may be public; no hostnames,
IP addresses, raw session IDs, paths, prompts, tokens or command lines.
No agent-control operations exist.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit

from export_live_snapshot import snapshot, _read, _instant, _recent, public_node_label

ORIGIN = "https://hungtranbkit.github.io"
PREFIX = "/projectflow-live/"
AGENT_TYPES = ("claude", "codex", "shell", "other")
CAPACITIES = ("healthy", "overloaded", "draining", "unknown")
MAX_STREAM_CLIENTS = 18
SEM = threading.BoundedSemaphore(MAX_STREAM_CLIENTS)


def _age_seconds(value, now):
    event = _instant(value)
    if event is None:
        return None
    age = int((now - event).total_seconds())
    return max(0, min(age, 86400 * 7)) if age >= 0 else None


def _agent(value):
    v = str(value or "").lower()
    return v if v in AGENT_TYPES[:3] else "other"


def detailed_snapshot(nodes_db: Path, sessions_db: Path, now=None):
    now = now or datetime.now(timezone.utc)
    base = snapshot(nodes_db, sessions_db, now=now)
    nr = _read(nodes_db, "SELECT id,last_known_status,last_heartbeat_at,"
               "capacity_status,execution_health_state FROM nodes ORDER BY id")
    sr = _read(sessions_db, "SELECT node_id,agent_type,last_activity_at,last_seen_at "
               "FROM session_records WHERE status='ACTIVE' AND deleted_at IS NULL")
    ids = {str(x["id"]): i for i, x in enumerate(nr, 1)}
    public_names = {key: public_node_label(key, index) for key, index in ids.items()}
    per_node_types = {}
    per_node_fresh60 = Counter()
    session_details = []
    total_types = Counter()
    total_minute = 0
    for row in sr:
        key = str(row["node_id"])
        agent = _agent(row["agent_type"])
        age = _age_seconds(row["last_activity_at"], now)
        recent = age is not None and age <= 300
        under_minute = age is not None and age <= 60
        total_types[agent] += 1
        if under_minute:
            total_minute += 1
            per_node_fresh60[key] += 1
        per_node_types.setdefault(key, Counter())[agent] += 1
        if len(session_details) < 24 and key in ids:
            if age is None:
                bucket = "unknown"
            elif age <= 60:
                bucket = "under_1m"
            elif age <= 300:
                bucket = "1_to_5m"
            else:
                bucket = "over_5m"
            session_details.append({
                "label": "Session %02d" % (len(session_details) + 1),
                "node": public_names[key],
                "agent_type": agent,
                "activity_bucket": bucket,
                "running_verified": None,  # Registry signals do NOT verify coding.
            })
    statuses = Counter()
    for item, node in zip(base["nodes"]["items"], nr):
        key = str(node["id"])
        raw = str(node["last_known_status"] or "").lower()
        health = str(node["execution_health_state"] or "").upper()
        fresh = _recent(node["last_heartbeat_at"], now, 300)
        if raw == "offline":
            status = "offline"
        elif raw == "online" and not fresh:
            status = "unknown"
        elif raw == "online" and health == "EXECUTION_OK":
            status = "online"
        elif raw == "online":
            status = "degraded"
        else:
            status = "unknown"
        capacity = str(node["capacity_status"] or "").lower()
        if capacity not in CAPACITIES:
            capacity = "unknown"
        item.update({
            "status": status,
            "capacity": capacity,
            "heartbeat_age_seconds": _age_seconds(node["last_heartbeat_at"], now),
            "signals_last_minute": per_node_fresh60[key],
            "agent_types": {k: per_node_types.get(key, Counter())[k] for k in AGENT_TYPES},
        })
        statuses[status] += 1
    base["schema_version"] = 2
    base["nodes"].update({
        "online": statuses["online"],
        "degraded": statuses["degraded"],
        "offline": statuses["offline"],
        "unknown": statuses["unknown"],
    })
    base["sessions"].update({
        "recent_last_minute": total_minute,
        "agent_types": {k: total_types[k] for k in AGENT_TYPES},
        "items": session_details,
    })
    base["fresh_window_seconds"] = 300
    base["transport"] = {"kind": "sse", "poll_seconds": 3, "state": "connected"}
    base["notes"].append(
        "Dữ liệu cập nhật mỗi 3 giây, nhưng độ trễ nguồn phụ thuộc heartbeat Terminal MCP."
    )
    return base


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, nodes_db, sessions_db):
        self.nodes_db = nodes_db
        self.sessions_db = sessions_db
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ProjectFlowPublicView/1"

    def _headers(self, status, content_type, content_length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, no-transform")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.end_headers()

    def _json(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)
        self.wfile.flush()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == PREFIX + "health":
            self._json(200, {"ok": True, "read_only": True})
            return
        if path == PREFIX + "snapshot":
            try:
                data = detailed_snapshot(self.server.nodes_db, self.server.sessions_db)
                self._json(200, data)
            except Exception:  # Do not leak filesystem/SQLite details to public.
                self._json(503, {"kind": "unknown", "error": "source_unavailable"})
            return
        if path != PREFIX + "events":
            self._json(404, {"error": "not_found"})
            return
        if not SEM.acquire(blocking=False):
            self._json(429, {"error": "stream_capacity"})
            return
        try:
            self._headers(200, "text/event-stream; charset=utf-8")
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            # Bound each connection. EventSource auto-reconnects as needed.
            for _ in range(24):
                try:
                    data = detailed_snapshot(self.server.nodes_db, self.server.sessions_db)
                    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(("event: status\ndata: " + payload + "\n\n").encode("utf-8"))
                except Exception:
                    self.wfile.write(b"event: unavailable\ndata: {}\n\n")
                self.wfile.flush()
                time.sleep(3)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            self.close_connection = True
            SEM.release()

    def do_POST(self):
        self._json(405, {"error": "read_only"})
    def do_PUT(self):
        self._json(405, {"error": "read_only"})
    def do_DELETE(self):
        self._json(405, {"error": "read_only"})
    def log_message(self, fmt, *args):
        # Do not log URL/query strings, request headers, tokens or client IP.
        return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes-db", type=Path, required=True)
    ap.add_argument("--sessions-db", type=Path, required=True)
    ap.add_argument("--port", type=int, default=18772)
    args = ap.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("invalid port")
    server = Server(("127.0.0.1", args.port), args.nodes_db, args.sessions_db)
    print("LIVE_AGGREGATE_SERVER_READY port=%d" % args.port, flush=True)
    server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
