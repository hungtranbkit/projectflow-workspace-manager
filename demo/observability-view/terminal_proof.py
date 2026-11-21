"""Bounded, read-only, fail-closed Terminal MCP runtime evidence for the public view.

No prompt, command, terminal output, credential, path or raw task label is returned.
The adapter is opt-in; a missing node, auth failure, idle pane or unchanged
heartbeat is never a verified running process.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,89}$")
_RUNNING_AGE = 30.0
_SOURCE_TTL = 20.0

# Fixed, reviewed public labels. Never publish arbitrary user-supplied titles.
_PUBLIC_TASKS = (
    ("crommacode", "Xây dựng nguồn bằng chứng hoạt động agent"),
    ("observability", "Hoàn thiện giám sát hoạt động agent"),
    ("woodpecker", "Kiểm thử hệ thống CI nội bộ"),
    ("autoreply", "Kiểm thử trả lời tự động Zalo"),
    ("zalo", "Hoàn thiện chuyển tiếp tin nhắn Zalo"),
    ("ota", "Kiểm thử OTA ESP32"),
    ("mesflow", "Kiểm thử quy trình MESFlow"),
)


def _timestamp(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _public_title(raw):
    if not isinstance(raw, str) or len(raw) > 140:
        return None
    normalized = raw.casefold()
    return next((label for needle, label in _PUBLIC_TASKS if needle in normalized), None)


def read_explicit_task_label(node_id, session, *, db_path=None):
    """Read only operator-assigned labels; no prompt-derived label is published."""
    if not isinstance(node_id, str) or not isinstance(session, str):
        return None
    if not _SAFE_NAME.fullmatch(node_id) or not _SAFE_NAME.fullmatch(session):
        return None
    path = Path(db_path) if db_path else Path.home() / ".local/state/terminal-mcp/audit.db"
    try:
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=0.2) as db:
            row = db.execute(
                "SELECT summary,source,updated_at FROM session_task_labels "
                "WHERE session=? AND node_id IN (?, 'local') "
                "ORDER BY updated_at DESC LIMIT 1", (session, node_id)
            ).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return None
    if not row or row[1] != "title":
        return None
    title = _public_title(row[0])
    return {"title": title, "at": row[2]} if title else None


def build_terminal_reader():
    """Use only already-configured local / authenticated Terminal MCP readers.

    Credential values are never returned, logged, cached into telemetry, or
    exposed to the public HTTP API.
    """
    from terminal_mcp.config import load_config
    from terminal_mcp.core import TerminalService
    from terminal_mcp.node_client import RemoteNodeClient
    from terminal_mcp.connection_store import ConnectionStore

    cfg = load_config()
    local = TerminalService(cfg, registry_node_id="hp-linux")
    remotes = {}
    for n in cfg.nodes.remote_nodes:
        token = os.environ.get(n.token_env) if n.token_env else None
        if token:
            remotes[n.node_id] = RemoteNodeClient(n.endpoint, token, timeout=2.0)
    for saved in ConnectionStore().list():
        if saved.node_id in remotes or not saved.token_file:
            continue
        token = ConnectionStore.read_token(ConnectionStore(), saved.token_file)
        if token:
            remotes[saved.node_id] = RemoteNodeClient(saved.endpoint, token, timeout=2.0)

    def read(node_id, session):
        if node_id == "hp-linux":
            return local.terminal_status(session)
        client = remotes.get(node_id)
        return client.status(session, timeout_seconds=2.0) if client else {}
    return read


class TerminalProofMonitor:
    """Only a witnessed output delta while a node is RUNNING verifies execution.

    A direct IDLE/WAITING_INPUT observation may be displayed without pretending
    there was a new code checkpoint. After 30s without changed output, running
    falls back to OPEN_UNVERIFIED. Sampling is shared across SSE subscribers.
    """
    def __init__(self, reader, *, interval=8.0, clock=time.monotonic, task_reader=None):
        self.reader = reader
        self.task_reader = task_reader or read_explicit_task_label
        self.interval = interval
        self.clock = clock
        self.lock = threading.Lock()
        self.last_poll = float("-inf")
        self.last_result = {}
        self.last_hash = {}
        self.proof_at = {}

    def snapshot(self, rows, *, now=None):
        now = now or datetime.now(timezone.utc)
        tick = self.clock()
        with self.lock:
            if tick - self.last_poll < self.interval:
                return self._fresh(now)
            self.last_poll = tick
            names = []
            for row in rows[:100]:
                node, name = str(row["node_id"]), str(row["session_name"])
                if _SAFE_NAME.fullmatch(node) and _SAFE_NAME.fullmatch(name):
                    names.append((node, name))
            observed = {}
            with ThreadPoolExecutor(max_workers=8) as executor:
                futs = {executor.submit(self.reader, node, name): (node, name)
                        for node, name in names}
                for fut in as_completed(futs):
                    key = futs[fut]
                    try:
                        value = fut.result()
                    except Exception:
                        continue
                    if not isinstance(value, dict) or value.get("error") or value.get("exists") is False or value.get("allowed") is False:
                        continue
                    raw = str(value.get("state") or "").upper()
                    at = now.isoformat(timespec="seconds")
                    record = None
                    if value.get("input_required") or raw in ("WAITING_INPUT", "WAITING"):
                        record = {"state": "WAITING_INPUT", "evidence": "node_observed", "observed_at": at}
                    elif raw == "IDLE":
                        record = {"state": "IDLE", "evidence": "node_observed", "observed_at": at}
                    elif raw == "BLOCKED":
                        record = {"state": "BLOCKED", "evidence": "node_observed", "observed_at": at}
                    elif raw == "RUNNING":
                        # The raw pane is not published, even as a preview.
                        output = value.get("last_output")
                        if isinstance(output, str) and output:
                            digest = hashlib.sha256(output[-50000:].encode("utf-8", "replace")).digest()
                            old = self.last_hash.get(key)
                            self.last_hash[key] = digest
                            if old is not None and old != digest:
                                self.proof_at[key] = now
                            age = (now - self.proof_at[key]).total_seconds() if key in self.proof_at else 10**9
                            if 0 <= age <= _RUNNING_AGE:
                                record = {"state": "RUNNING_VERIFIED", "evidence": "terminal_output_delta",
                                          "observed_at": at, "checkpoint_at": self.proof_at[key].isoformat(timespec="seconds")}
                    if record is not None:
                        # An explicit owner title is trusted metadata, not a proof of activity.
                        label = self.task_reader(*key)
                        if label and label.get("title") and _timestamp(label.get("at")):
                            record["task_hint"] = label["title"]
                            record["task_source"] = "trusted_task_label"
                        observed[key] = record
            self.last_result = observed
            return self._fresh(now)

    def _fresh(self, now):
        valid = {}
        for key, value in self.last_result.items():
            at = _timestamp(value.get("observed_at"))
            age = (now - at).total_seconds() if at else 10**9
            if age < 0 or age > _SOURCE_TTL:
                continue
            result = dict(value)
            if result["state"] == "RUNNING_VERIFIED":
                checkpoint = _timestamp(result.get("checkpoint_at"))
                if checkpoint is None or not (0 <= (now - checkpoint).total_seconds() <= _RUNNING_AGE):
                    continue
            valid[key] = result
        return valid
