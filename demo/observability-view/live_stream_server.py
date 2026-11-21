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
from public_session_detail import classify_public_session
from terminal_proof import TerminalProofMonitor, build_terminal_reader

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


def _live_fleet_rows(registry_rows, observation, allowed_nodes):
    """Canonicalize HP's legacy 'local' alias and read only live node lists.

    The registry supplies optional Git/agent metadata; it does NOT claim an
    ACTIVE row is still open. An unavailable node is tracked as uncovered.
    """
    if not isinstance(observation, dict) or not isinstance(observation.get("sessions"), list):
        return list(registry_rows), None
    coverage = observation.get("covered_nodes")
    if not isinstance(coverage, list) or not coverage:
        return list(registry_rows), None
    known = {}
    for raw in registry_rows:
        row = dict(raw)
        node = "hp-linux" if row["node_id"] == "local" else row["node_id"]
        key = (node, row["session_name"])
        # The canonical HP record wins over a legacy 'local' duplicate.
        if key not in known or raw["node_id"] != "local":
            row["node_id"] = node
            known[key] = row
    discovered = {}
    for item in observation["sessions"][:100]:
        if not isinstance(item, dict):
            continue
        node, name = item.get("node_id"), item.get("session_name")
        if node not in allowed_nodes or not isinstance(name, str) or not (
                3 <= len(name) <= 90 and name[0].isalnum() and
                all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in name)):
            continue
        row = dict(known.get((node, name), {}))
        row.update(node_id=node, session_name=name)
        for field in ("agent_type", "last_activity_at", "last_seen_at",
                      "last_known_state", "last_checkpoint_at", "last_commit",
                      "git_branch", "stable_session_id", "generation"):
            row.setdefault(field, None)
        # Tie proof to an actual pane creation epoch as well as registry
        # stable ID, so a recreated remote session cannot inherit old proof.
        creation = item.get("instance_hint")
        if isinstance(creation, str) and creation:
            row["stable_session_id"] = str(row["stable_session_id"] or "") + "|" + creation
        discovered[(node, name)] = row
    return [discovered[key] for key in sorted(discovered)], {
        "covered_nodes": [x for x in coverage if x in allowed_nodes],
        "unreachable_nodes": [x for x in observation.get("unreachable_nodes", [])
                              if x in allowed_nodes],
    }


def detailed_snapshot(nodes_db: Path, sessions_db: Path, now=None, monitor=None):
    now = now or datetime.now(timezone.utc)
    base = snapshot(nodes_db, sessions_db, now=now)
    nr = _read(nodes_db, "SELECT id,last_known_status,last_heartbeat_at,"
               "capacity_status,execution_health_state FROM nodes ORDER BY id")
    sr = _read(sessions_db,
               "SELECT node_id,agent_type,last_activity_at,last_seen_at,"
               "session_name,last_known_state,last_checkpoint_at,last_commit,git_branch,"
               "stable_session_id,generation "
               "FROM session_records WHERE status='ACTIVE' AND deleted_at IS NULL ORDER BY node_id,session_name")
    ids = {str(x["id"]): i for i, x in enumerate(nr, 1)}
    fleet_coverage = None
    source = getattr(getattr(monitor, "reader", None), "list_sessions", None)
    if callable(source):
        try:
            sr, fleet_coverage = _live_fleet_rows(sr, source(), set(ids))
        except Exception:  # Unreachable controller must not break the read-only view.
            fleet_coverage = None
    # Observations are privately authenticated node readbacks; only their
    # strict public-safe projection is ever merged into the output payload.
    observed = monitor.snapshot(sr, now=now) if monitor is not None else {}
    public_names = {key: public_node_label(key, index) for key, index in ids.items()}
    per_node_types = {}
    per_node_fresh60 = Counter()
    per_node_fresh5 = Counter()
    per_node_count = Counter()
    session_details = []
    total_types = Counter()
    total_minute = 0
    total_fresh5 = 0
    projects = Counter()
    for row in sr:
        key = str(row["node_id"])
        agent = _agent(row["agent_type"])
        per_node_count[key] += 1
        summary = classify_public_session(
            row["session_name"], state=row["last_known_state"],
            checkpoint_at=row["last_checkpoint_at"],
            last_activity_at=row["last_activity_at"],
            last_commit=row["last_commit"], git_branch=row["git_branch"], now=now)
        proof = observed.get((key, row["session_name"]))
        if proof and proof.get("state") in ("IDLE", "WAITING_INPUT", "BLOCKED", "RUNNING_VERIFIED"):
            summary["execution_state"] = proof["state"]
            summary["evidence"] = proof["evidence"]
            summary["running_verified"] = True if proof["state"] == "RUNNING_VERIFIED" else None
            summary["last_checkpoint_at"] = proof.get("checkpoint_at")
            if proof.get("task_source") == "trusted_task_label":
                summary["task_hint"] = proof["task_hint"]
                summary["task_source"] = "trusted_task_label"
        # With authenticated node monitoring, only a new output delta is an
        # execution activity signal. A registry heartbeat is never progress.
        if monitor is not None:
            age = (_age_seconds(proof.get("checkpoint_at"), now)
                   if proof and proof.get("evidence") == "terminal_output_delta" else None)
            if proof and proof.get("agent_type") in AGENT_TYPES:
                agent = proof["agent_type"]
            if age is not None:
                summary["last_activity_at"] = proof["checkpoint_at"]
        else:
            age = _age_seconds(row["last_activity_at"], now)
        if age is not None and age <= 300:
            total_fresh5 += 1
            per_node_fresh5[key] += 1
        if age is not None and age <= 60:
            total_minute += 1
            per_node_fresh60[key] += 1
        total_types[agent] += 1
        per_node_types.setdefault(key, Counter())[agent] += 1
        projects[summary["project"]] += 1
        if key in ids:
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
                "running_verified": summary["running_verified"],
                "session": summary["session"],
                "project": summary["project"],
                "task_hint": summary["task_hint"],
                "task_source": summary["task_source"],
                "execution_state": summary["execution_state"],
                "evidence": summary["evidence"],
                "commit_ref": summary["commit_ref"],
                "branch_ref": summary["branch_ref"],
                "last_activity_at": summary["last_activity_at"],
                "last_checkpoint_at": summary["last_checkpoint_at"],
            })
    # Rank every registered session BEFORE truncating the public list.
    # A heartbeat never upgrades OPEN_UNVERIFIED to RUNNING_VERIFIED.
    rank = {"RUNNING_VERIFIED": 0, "WAITING_INPUT": 1, "BLOCKED": 2,
            "STALLED": 3, "IDLE": 4, "OPEN_UNVERIFIED": 5, "EXITED": 6}
    session_details.sort(key=lambda item: (
        rank.get(item["execution_state"], 7),
        _age_seconds(item["last_checkpoint_at"], now) if
            _age_seconds(item["last_checkpoint_at"], now) is not None else 99999999,
        _age_seconds(item["last_activity_at"], now) if
            _age_seconds(item["last_activity_at"], now) is not None else 99999999,
        item["project"], item["node"], item["session"] or "",
    ))
    proven_running = sum(item["running_verified"] is True for item in session_details)
    visible_sessions = session_details[:24]
    for n, item in enumerate(visible_sessions, 1):
        item["label"] = "Session %02d" % n
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
            "open_sessions": per_node_count[key],
            "recent_activity_signals": per_node_fresh5[key],
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
        "open": len(sr),
        "recent_activity_signals": total_fresh5,
        "recent_last_minute": total_minute,
        "agent_types": {k: total_types[k] for k in AGENT_TYPES},
        "items": visible_sessions,
        "displayed_count": len(visible_sessions),
        "hidden_count": len(session_details) - len(visible_sessions),
        "projects": [{"name": name, "open_sessions": count}
                     for name, count in sorted(projects.items())],
        "proven_running": proven_running,
        "running_verification": "verified_recent_checkpoint_or_live_output_delta",
        "observation_source": ("terminal_mcp_authenticated_fleet" if fleet_coverage
                               else "terminal_mcp_authenticated_status" if monitor is not None
                               else "registry_only"),
        "coverage": fleet_coverage,
    })
    base["fresh_window_seconds"] = 300
    base["transport"] = {"kind": "sse", "poll_seconds": 3, "state": "connected"}
    base["notes"].append(
        "Dữ liệu cập nhật mỗi 3 giây nhưng heartbeat hoặc phiên mở không chứng minh đang code. "
        "Tên task lấy từ quy tắc nhận dạng tên session, không phải task đã nghiệm thu."
    )
    return base


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, nodes_db, sessions_db, monitor=None):
        self.nodes_db = nodes_db
        self.sessions_db = sessions_db
        self.monitor = monitor
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
                data = detailed_snapshot(self.server.nodes_db, self.server.sessions_db, monitor=self.server.monitor)
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
                    data = detailed_snapshot(self.server.nodes_db, self.server.sessions_db, monitor=self.server.monitor)
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
    ap.add_argument("--enable-terminal-proof", action="store_true",
                    help="Opt-in authenticated read-only session status sampling")
    args = ap.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("invalid port")
    monitor = TerminalProofMonitor(build_terminal_reader()) if args.enable_terminal_proof else None
    server = Server(("127.0.0.1", args.port), args.nodes_db, args.sessions_db, monitor=monitor)
    print("LIVE_AGGREGATE_SERVER_READY port=%d" % args.port, flush=True)
    server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
