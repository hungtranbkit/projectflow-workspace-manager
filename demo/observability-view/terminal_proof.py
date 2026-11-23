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
_FOREGROUND = re.compile(r"current command is '([a-z0-9._-]{1,40})'", re.I)
# A trusted node sometimes reports UNKNOWN while a real foreground tool is
# running, because its earlier activity timestamp is stale. Requiring TWO
# different output samples still verifies execution without trusting heartbeat.
_ACTIVE_COMMANDS = frozenset({
    "python", "python3", "pytest", "node", "npm", "pnpm", "npx",
    "codex", "claude", "java", "go", "cargo", "rustc", "uv",
    "bash-script", "gcc", "g++", "make", "cmake"
})


# Public values are fixed strings, NEVER excerpts of arbitrary terminal data.
# These indicate patterns visible in recent CLI output, not proof that a test
# passed, a file was changed or the assignment has been completed.
_SAFE_ACTIVITY_RULES = (
    (re.compile(r"^(?:running|starting|executing)\s+(?:(?:unit|integration|smoke|e2e)\s+)?tests?\b", re.I),
     "Có dòng báo đang chạy kiểm thử"),
    (re.compile(r"^(?:pytest|python(?:3(?:\.\d+)?)?\s+-m\s+pytest)\b", re.I),
     "Có dòng lệnh kiểm thử Python"),
    (re.compile(r"^(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:test|check|lint)\b", re.I),
     "Có dòng lệnh kiểm thử JavaScript"),
    (re.compile(r"^(?:collecting\b|collected\s+\d{1,6}\s+items?\b)", re.I),
     "Có dòng báo thu thập bài kiểm thử"),
    (re.compile(r"^\d{1,6}\s+passed\b", re.I),
     "Có dòng báo kết quả kiểm thử PASS"),
    (re.compile(r"^\d{1,6}\s+failed\b", re.I),
     "Có dòng báo kiểm thử thất bại"),
    (re.compile(r"^(?:building|compiling|starting build|running build|npm\s+run\s+build|pnpm\s+build|yarn\s+build)\b", re.I),
     "Có dòng báo build hoặc biên dịch"),
    (re.compile(r"^(?:applying patch|applied patch|editing file|edited file|updated file|modified file)\b", re.I),
     "Có dòng báo thao tác chỉnh sửa file"),
    (re.compile(r"^(?:reading source|reading code|inspecting source|analyzing code|searching code)\b", re.I),
     "Có dòng báo đọc hoặc phân tích mã nguồn"),
    (re.compile(r"^(?:running command|executing command|running tool|executing tool)\b", re.I),
     "Có dòng báo thực thi công cụ"),
    (re.compile(r"^(?:error:|traceback \(most recent call last\):)", re.I),
     "Có dòng thông báo lỗi cần kiểm tra"),
)
PUBLIC_ACTIVITY_LABELS = frozenset(label for _, label in _SAFE_ACTIVITY_RULES)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def safe_recent_activity(output):
    """Map at most the last eight terminal lines to <=3 *fixed* public hints.

    No terminal source text, paths, commands, secrets, file names, task
    prompts or arbitrary identifiers survive. An empty list is an honest
    result when the output contains no approved, meaningful work signals.
    """
    if not isinstance(output, str):
        return []
    recent = [x.strip() for x in _ANSI_ESCAPE.sub("", output[-4096:]).splitlines()
              if x.strip()][-8:]
    result = []
    for line in reversed(recent):
        if not line or len(line) > 280:
            continue
        for pattern, safe_label in _SAFE_ACTIVITY_RULES:
            if pattern.search(line):
                if safe_label not in result:
                    result.append(safe_label)
                break
        if len(result) >= 3:
            break
    return result


# A tmux screen may redraw only an elapsed timer/spinner for hours. Those
# redraws cannot prove that work is progressing.
_BACKGROUND_WAIT = re.compile(
    r"Waiting for background terminal\s*\(\s*(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?",
    re.I,
)


def _is_long_background_wait(output):
    if not isinstance(output, str):
        return False
    found = _BACKGROUND_WAIT.search(output[-4096:])
    if not found:
        return False
    parts = [int(item) if item is not None else 0 for item in found.groups()]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] >= 15 * 60


def _meaningful_activity_digest(output):
    """Only vetted event categories/counts, never UI timers or private strings.

    This digest stays in process memory; no raw line or identifier is returned
    to public SSE. The whitelist intentionally favors false negatives over
    showing an animated spinner as code progress.
    """
    if not isinstance(output, str):
        return None
    events = []
    for line in _ANSI_ESCAPE.sub("", output[-4096:]).splitlines()[-12:]:
        line = line.strip()
        if not line or len(line) > 280:
            continue
        for pattern, label in _SAFE_ACTIVITY_RULES:
            if pattern.search(line):
                count = re.match(r"^(\d{1,6})\s+(?:passed|failed)\b", line, re.I)
                events.append(label + (":" + count.group(1) if count else ""))
                break
    return hashlib.sha256("\n".join(events[-8:]).encode("utf-8")).digest() if events else None


def _unknown_but_foreground(status):
    reason = status.get("reason")
    match = _FOREGROUND.search(reason) if isinstance(reason, str) else None
    return bool(match and match.group(1).casefold() in _ACTIVE_COMMANDS)

def _agent_ui_busy(status):
    """Trust an actual Claude/Codex in-flight UI marker, not tmux activity.

    Terminal MCP classifies the current CLI's *footer* or agent adapter.
    We additionally require the foreground command and agent type to match:
    an old 'esc to interrupt' line in a bash scrollback is not proof.
    A UI-busy report proves only an executing turn, never a code edit.
    """
    if not isinstance(status, dict) or str(status.get("state") or "").upper() != "RUNNING":
        return False
    if status.get("input_required") is True:
        return False
    agent = str(status.get("agent_type") or "").casefold()
    reason = status.get("reason")
    if agent not in ("claude", "codex") or not isinstance(reason, str):
        return False
    command = _FOREGROUND.search(reason)
    if not command or command.group(1).casefold() != agent:
        return False
    return (reason.startswith("agent CLI busy footer matched ")
            or reason.startswith(agent + " adapter reports a turn in flight"))


# Fixed, reviewed public labels. Never publish arbitrary user-supplied titles.
_PUBLIC_TASKS = (
    ("crommacode", "Xây dựng nguồn bằng chứng hoạt động agent"),
    ("observability", "Hoàn thiện giám sát hoạt động agent"),
    ("woodpecker", "Kiểm thử hệ thống CI nội bộ"),
    ("qualitygate", "Kiểm soát chất lượng mã nguồn"),
    ("quality gate", "Kiểm soát chất lượng mã nguồn"),
    ("autoreply", "Kiểm thử trả lời tự động Zalo"),
    ("zalo", "Hoàn thiện chuyển tiếp tin nhắn Zalo"),
    ("ota", "Kiểm thử OTA ESP32"),
    ("esp32", "Kiểm thử thiết bị ESP32"),
    ("mesflow", "Kiểm thử quy trình MESFlow"),
    ("brandagent", "Triển khai Brand Agent"),
    ("brand agent", "Triển khai Brand Agent"),
    ("screen remote", "Kiểm thử Screen Remote"),
    ("screenremote", "Kiểm thử Screen Remote"),
    ("remoteops", "Kiểm thử RemoteOps"),
    ("novaretail", "Kiểm thử NovaRetail"),
    ("projectflow", "Điều phối công việc ProjectFlow"),
)


def _read_authenticated_task_label(client, session, *, on_unsupported=None):
    """Only an explicit owner title from a bearer-authenticated node.

    The node itself verifies session existence/read access/creation age.
    Old agents lacking the GET route simply yield None; never read remote
    raw transcripts or infer a task from another session's name.
    """
    getter = getattr(client, "get_task_label", None)
    if not isinstance(session, str) or not _SAFE_NAME.fullmatch(session):
        return None
    if not callable(getter):
        return on_unsupported() if callable(on_unsupported) else None
    try:
        response = getter(session, timeout_seconds=1.5)
    except Exception as exc:
        # HTTP 404 is the exact backward-compatibility signal of a node
        # without this optional route. Auth refusal/transport failure are
        # NEVER reasons to expose an older cached controller label.
        if getattr(exc, "http_status", None) == 404 and callable(on_unsupported):
            return on_unsupported()
        return None
    if not isinstance(response, dict) or response.get("status") != "OK":
        return None
    label = response.get("label")
    if not isinstance(label, dict) or label.get("source") != "title":
        return None
    title = _public_title(label.get("summary"))
    at = label.get("updated_at")
    if not title or _timestamp(at) is None:
        return None
    return {"title": title, "at": at}


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


def _label_is_current_instance(label, stable_id):
    """Never attribute a former tmux session's task to its replacement."""
    if not isinstance(label, dict) or not label.get("title"):
        return False
    observed = _timestamp(label.get("at"))
    if observed is None:
        return False
    if not isinstance(stable_id, str) or "|" not in stable_id:
        return True  # The authorized node GET already checks its instance.
    creation = stable_id.rpartition("|")[2]
    born = _timestamp(creation)
    if born is not None:
        created = born.timestamp()
    else:
        try:
            created = float(creation)
        except (TypeError, ValueError, OverflowError):
            return False
    return observed.timestamp() + 5 >= created


def read_explicit_task_label(node_id, session, *, db_path=None):
    """Read only operator-assigned labels; no prompt-derived label is published."""
    if not isinstance(node_id, str) or not isinstance(session, str):
        return None
    if not _SAFE_NAME.fullmatch(node_id) or not _SAFE_NAME.fullmatch(session):
        return None
    path = Path(db_path) if db_path else Path.home() / ".local/state/terminal-mcp/audit.db"
    try:
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=0.2) as db:
            # A node-local mirror is valid only for THIS HP collector's
            # own sessions. Never borrow a local title for a remote node
            # with an identically named session.
            if node_id == "hp-linux":
                where = "node_id IN ('hp-linux', 'local')"
                params = (session,)
            else:
                where = "node_id=?"
                params = (session, node_id)
            row = db.execute(
                "SELECT summary,source,updated_at FROM session_task_labels "
                "WHERE session=? AND " + where +
                " ORDER BY updated_at DESC LIMIT 1", params
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
    store = ConnectionStore()
    remotes = {}
    for n in cfg.nodes.remote_nodes:
        token = os.environ.get(n.token_env) if n.token_env else None
        if token:
            remotes[n.node_id] = RemoteNodeClient(n.endpoint, token, timeout=2.0)
    for saved in store.list():
        if saved.node_id in remotes or not saved.token_file:
            continue
        token = store.read_token(saved.token_file)
        if token:
            remotes[saved.node_id] = RemoteNodeClient(saved.endpoint, token, timeout=2.0)

    def read(node_id, session):
        if node_id == "hp-linux":
            return local.terminal_status(session)
        client = remotes.get(node_id)
        return client.status(session, timeout_seconds=2.0) if client else {}

    def screen_digest(node_id, session):
        """Privately fingerprint the bounded FULL tmux screen, not last 20 rows.

        The node's authenticated read-only tail API returns an already
        redacted, capped view. Only an opaque SHA-256 digest is retained by
        this collector. Never return, log or export the screen contents.
        Called only for a potential RUNNING foreground command.
        """
        if node_id == "hp-linux":
            result = local.terminal_tail(session, lines=80)
        else:
            client = remotes.get(node_id)
            result = client.tail(session, lines=80) if client else None
        if not isinstance(result, dict) or result.get("error"):
            return None
        output = result.get("output")
        if not isinstance(output, str) or not output:
            return None
        return hashlib.sha256(output[-65536:].encode("utf-8", "replace")).digest()
    read.screen_digest = screen_digest

    # The fleet's currently open sessions come from authenticated node LIST
    # responses, not stale/duplicated controller registry rows. Never read
    # terminal output while listing, and never expose the auth connection.
    listing = {"sessions": [], "covered_nodes": [], "unreachable_nodes": []}
    list_lock = threading.Lock()
    listed_at = [float("-inf")]

    def _list_one(node, client):
        result = (local.terminal_list_sessions() if node == "hp-linux" else
                  client.list_sessions(timeout_seconds=2.0))
        if not isinstance(result, dict) or not isinstance(result.get("sessions"), list):
            raise ValueError("unexpected live session list")
        values = []
        for item in result["sessions"][:60]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and _SAFE_NAME.fullmatch(name):
                # created is only an internal instance discriminator.
                created = item.get("created")
                hint = str(created)[:80] if isinstance(created, (int, float, str)) else ""
                values.append({"node_id": node, "session_name": name, "instance_hint": hint})
        return values

    def list_sessions():
        tick = time.monotonic()
        with list_lock:
            if tick - listed_at[0] < 12.0:
                return {"sessions": list(listing["sessions"]),
                        "covered_nodes": list(listing["covered_nodes"]),
                        "unreachable_nodes": list(listing["unreachable_nodes"])}
            nodes = {"hp-linux": None, **remotes}
            successes = []
            unavailable = []
            all_sessions = []
            with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
                futures = {pool.submit(_list_one, node, client): node
                           for node, client in nodes.items()}
                for future in as_completed(futures):
                    node = futures[future]
                    try:
                        rows = future.result()
                    except Exception:
                        unavailable.append(node)
                    else:
                        successes.append(node)
                        all_sessions.extend(rows)
            # Deduplicate across readbacks without relying on the registry.
            distinct = {(r["node_id"], r["session_name"]): r for r in all_sessions}
            listing.update(sessions=sorted(distinct.values(),
                                           key=lambda r: (r["node_id"], r["session_name"])),
                           covered_nodes=sorted(successes),
                           unreachable_nodes=sorted(unavailable))
            listed_at[0] = tick
            return {"sessions": list(listing["sessions"]),
                    "covered_nodes": list(listing["covered_nodes"]),
                    "unreachable_nodes": list(listing["unreachable_nodes"])}

    # An existing node's private task label is never forwarded as-is.
    # Only the server-owned, fixed public title can leave this process.
    label_lock = threading.Lock()
    label_cache = {}
    def task_label(node_id, session):
        if node_id == "hp-linux":
            return read_explicit_task_label(node_id, session)
        client = remotes.get(node_id)
        if client is None:
            return None
        tick = time.monotonic()
        key = (node_id, session)
        with label_lock:
            previous = label_cache.get(key)
            if previous and tick - previous[0] < 12.0:
                return previous[1]
        result = _read_authenticated_task_label(
            client, session, on_unsupported=lambda: read_explicit_task_label(node_id, session))
        with label_lock:
            if len(label_cache) > 160:
                label_cache.clear()
            label_cache[key] = (tick, result)
        return result
    read.list_sessions = list_sessions
    read.task_label = task_label
    return read


class TerminalProofMonitor:
    """Only a witnessed output delta while a node is RUNNING verifies execution.

    A direct IDLE/WAITING_INPUT observation may be displayed without pretending
    there was a new code checkpoint. After 30s without changed output, running
    falls back to OPEN_UNVERIFIED. Sampling is shared across SSE subscribers.
    """
    def __init__(self, reader, *, interval=8.0, clock=time.monotonic, task_reader=None):
        self.reader = reader
        self.task_reader = (task_reader or getattr(reader, "task_label", None)
                            or read_explicit_task_label)
        self.interval = interval
        self.clock = clock
        self.lock = threading.Lock()
        self.last_poll = float("-inf")
        self.last_result = {}
        self.last_hash = {}
        self.proof_at = {}
        self.last_semantic = {}
        self.semantic_proof_at = {}

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
                    # Never carry a proof across a deleted/recreated session.
                    # The registry increments generation on a revived session.
                    available = row.keys()
                    stable_id = str(row["stable_session_id"] or "") if "stable_session_id" in available else ""
                    generation = str(row["generation"] or "") if "generation" in available else ""
                    names.append((node, name, stable_id, generation))
            active = set(names)
            self.last_hash = {k: v for k, v in self.last_hash.items() if k in active}
            self.proof_at = {k: v for k, v in self.proof_at.items() if k in active}
            self.last_semantic = {k: v for k, v in self.last_semantic.items() if k in active}
            self.semantic_proof_at = {k: v for k, v in self.semantic_proof_at.items() if k in active}
            observed = {}
            def read_one(node, name):
                value = self.reader(node, name)
                if not isinstance(value, dict) or value.get("error") or value.get("exists") is False or value.get("allowed") is False:
                    return value, None, None
                raw = str(value.get("state") or "").upper()
                screen_hash = None
                if (raw == "RUNNING" or (raw == "UNKNOWN" and _unknown_but_foreground(value))) and not _agent_ui_busy(value):
                    screen = getattr(self.reader, "screen_digest", None)
                    if callable(screen):
                        try:
                            screen_hash = screen(node, name)
                        except Exception:
                            screen_hash = None
                try:
                    label = self.task_reader(node, name)
                except Exception:
                    label = None  # Title failure must not conceal a valid node state.
                return value, label, screen_hash

            # Both private HTTP reads run inside the same bounded worker.
            # A slow node label route can no longer serially block all other
            # node status observations or the public SSE response.
            with ThreadPoolExecutor(max_workers=8) as executor:
                futs = {executor.submit(read_one, node, name): (node, name, stable_id, generation)
                        for node, name, stable_id, generation in names}
                for fut in as_completed(futs):
                    node, name, stable_id, generation = futs[fut]
                    key = (node, name)
                    history_key = (node, name, stable_id, generation)
                    try:
                        value, label, screen_hash = fut.result()
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
                    elif raw == "RUNNING" or (raw == "UNKNOWN" and _unknown_but_foreground(value)):
                        # The raw pane stays private. A Claude/Codex busy footer
                        # proves only that a turn is in flight, NOT that its
                        # spinner, elapsed clock or background process advanced.
                        output = value.get("last_output")
                        if _agent_ui_busy(value):
                            self.last_hash.pop(history_key, None)
                            self.proof_at.pop(history_key, None)
                            if _is_long_background_wait(output):
                                self.semantic_proof_at.pop(history_key, None)
                                record = {"state": "STALLED", "evidence": "node_observed",
                                          "observed_at": at}
                            else:
                                digest = _meaningful_activity_digest(output)
                                old = self.last_semantic.get(history_key)
                                self.last_semantic[history_key] = digest
                                if digest is not None and old is not None and digest != old:
                                    self.semantic_proof_at[history_key] = now
                                proof_time = self.semantic_proof_at.get(history_key)
                                age = (now - proof_time).total_seconds() if proof_time else 10**9
                                if 0 <= age <= _RUNNING_AGE:
                                    record = {"state": "RUNNING_VERIFIED",
                                              "evidence": "terminal_output_delta",
                                              "observed_at": at,
                                              "checkpoint_at": proof_time.isoformat(timespec="seconds")}
                                else:
                                    record = {"state": "BUSY_UNVERIFIED", "evidence": "agent_ui_busy",
                                              "observed_at": at}
                        else:
                            if isinstance(screen_hash, bytes) and len(screen_hash) == 32:
                                kind, digest = "terminal_screen_delta", screen_hash
                            elif isinstance(output, str) and output:
                                kind = "terminal_output_delta"
                                digest = hashlib.sha256(output[-50000:].encode("utf-8", "replace")).digest()
                            else:
                                kind, digest = None, None
                            if digest is not None:
                                old = self.last_hash.get(history_key)
                                self.last_hash[history_key] = (kind, digest)
                                # Changing read source is not evidence.
                                if old is not None and old[0] == kind and old[1] != digest:
                                    self.proof_at[history_key] = (now, kind)
                                elif old is not None and old[0] != kind:
                                    self.proof_at.pop(history_key, None)
                                proof = self.proof_at.get(history_key)
                                age = (now - proof[0]).total_seconds() if proof else 10**9
                                if proof and proof[1] == kind and 0 <= age <= _RUNNING_AGE:
                                    record = {"state": "RUNNING_VERIFIED", "evidence": kind,
                                              "observed_at": at,
                                              "checkpoint_at": proof[0].isoformat(timespec="seconds")}
                    if raw in ("IDLE", "WAITING_INPUT", "WAITING", "BLOCKED") or value.get("input_required"):
                        # An ended turn invalidates proof before the same session's next run.
                        self.last_hash.pop(history_key, None)
                        self.proof_at.pop(history_key, None)
                        self.last_semantic.pop(history_key, None)
                        self.semantic_proof_at.pop(history_key, None)
                    # An explicitly assigned task is independent of liveness.
                    # UNKNOWN execution can have a verified label without
                    # becoming RUNNING or implying a new code checkpoint.
                    if _label_is_current_instance(label, stable_id):
                        if record is None:
                            record = {"state": "OPEN_UNVERIFIED", "evidence": "registry_only",
                                      "observed_at": at}
                        record["task_hint"] = label["title"]
                        record["task_source"] = "trusted_task_label"
                    if record is not None:
                        if record["state"] == "RUNNING_VERIFIED":
                            record["recent_actions"] = safe_recent_activity(value.get("last_output"))
                        # Node status is trusted for agent type, but never publish a
                        # guessed process name or arbitrary executable as an agent.
                        source_type = value.get("agent_type") or (value.get("resource") or {}).get("agent")
                        agent_type = str(source_type or "").casefold()
                        if agent_type in ("claude", "codex", "shell"):
                            record["agent_type"] = agent_type
                        # Task metadata cannot substitute for execution evidence.
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
