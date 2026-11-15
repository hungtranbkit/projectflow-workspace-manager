"""Fail-closed integration of existing ChatGPT monitors with ProjectFlow Harness.

The bridge does not create, enable, reschedule or delete ChatGPT Automations.
It pins the immutable goals of enabled monitors in SQLite, reads child
checkpoints, and can register a Terminal MCP durable task receipt ONLY after
local MCP server readback. It never claims RUNNING or DONE from a receipt.

Do not send passwords, API keys, Zalo data or other private content to this CLI.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.agent_harness_mvp import HarnessStore, HarnessError

HERE = Path(__file__).resolve().parents[1]
REGISTRY = HERE / "config" / "harness_monitor_registry.json"
STATE_DB = Path.home() / ".local/state/projectflow-harness/harness-mvp.db"
TERMINAL = {"DONE_VERIFIED", "FAILED", "EXPIRED"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,90}$")
TASK_RE = re.compile(r"^[a-zA-Z0-9_:-]{8,160}$")
VALID_AGENTS = {"codex", "claude", "shell"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def goal_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_registry(path: Path = REGISTRY) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise HarnessError("unsupported registry schema")
    rows = data.get("automations", [])
    if not isinstance(rows, list) or not rows:
        raise HarnessError("automation registry empty")
    mapping: dict[str, dict[str, Any]] = {}
    for entry in rows:
        ident = entry.get("automation_id")
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or ident in mapping:
            raise HarnessError("duplicate/unsafe automation ID")
        enabled = entry.get("configured_enabled") is True
        goal = entry.get("main_goal")
        if enabled and (not isinstance(goal, str) or not goal.strip()):
            raise HarnessError(f"active monitor {ident} lacks immutable main goal")
        if not enabled and entry.get("mode") != "paused":
            raise HarnessError("paused automation must not have execution mode")
        if enabled and entry.get("mode") not in {"observe_and_register", "observe_only"}:
            raise HarnessError("unexpected active execution mode")
        mapping[ident] = entry
    return mapping


def connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise HarnessError("Harness DB not installed: do not create a new production DB")
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS harness_monitor_bindings (
            automation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            main_goal TEXT,
            main_goal_sha256 TEXT NOT NULL,
            integration_mode TEXT NOT NULL,
            enabled_at_import INTEGER NOT NULL,
            installed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS harness_monitor_receipts (
            id INTEGER PRIMARY KEY,
            automation_id TEXT NOT NULL REFERENCES harness_monitor_bindings(automation_id),
            task_id INTEGER NOT NULL UNIQUE,
            external_task_id TEXT NOT NULL UNIQUE,
            session_name TEXT NOT NULL,
            backend_node TEXT NOT NULL,
            registered_at TEXT NOT NULL
        );
    """)


def bootstrap(registry: dict[str, dict[str, Any]], db_path: Path) -> dict[str, int]:
    """Idempotent import; never overwrites a goal pin or toggles paused jobs."""
    inserted = already = 0
    with connect(db_path) as db:
        init_schema(db)
        db.execute("BEGIN IMMEDIATE")
        for ident, entry in registry.items():
            goal = entry.get("main_goal") if entry["configured_enabled"] else None
            fingerprint = goal_sha(goal or "")
            mode = entry["mode"]
            existing = db.execute(
                "SELECT * FROM harness_monitor_bindings WHERE automation_id=?", (ident,)
            ).fetchone()
            if existing:
                if (existing["main_goal_sha256"] != fingerprint
                        or existing["integration_mode"] != mode
                        or existing["enabled_at_import"] != int(entry["configured_enabled"])):
                    raise HarnessError(f"immutable automation binding drift: {ident}")
                already += 1
                continue
            db.execute("""INSERT INTO harness_monitor_bindings
                (automation_id,title,main_goal,main_goal_sha256,integration_mode,
                 enabled_at_import,installed_at) VALUES (?,?,?,?,?,?,?)""",
                (ident, entry["title"], goal, fingerprint, mode,
                 int(entry["configured_enabled"]), now().isoformat()))
            inserted += 1
    return {"registered": inserted, "already": already, "total": len(registry)}


def require_binding(
    automation_id: str, registry: dict[str, dict[str, Any]], db: sqlite3.Connection
) -> tuple[dict[str, Any], sqlite3.Row]:
    entry = registry.get(automation_id)
    if not entry:
        raise HarnessError("automation not registered in reviewed source")
    existing = db.execute(
        "SELECT * FROM harness_monitor_bindings WHERE automation_id=?", (automation_id,)
    ).fetchone()
    if not existing:
        raise HarnessError("monitor binding not bootstrapped")
    expected_goal = entry.get("main_goal") if entry["configured_enabled"] else None
    if (existing["main_goal_sha256"] != goal_sha(expected_goal or "")
            or existing["integration_mode"] != entry["mode"]
            or existing["enabled_at_import"] != int(entry["configured_enabled"])):
        raise HarnessError("GOAL_DRIFT: binding changed; refuse action")
    return entry, existing


def status(
    registry: dict[str, dict[str, Any]], db_path: Path, ident: str | None
) -> dict[str, Any]:
    store = HarnessStore(db_path)
    with connect(db_path) as db:
        init_schema(db)
        ids = [ident] if ident else list(registry)
        rows = []
        for target in ids:
            entry, binding = require_binding(target, registry, db)
            goal = binding["main_goal"]
            tasks = store.list_tasks(main_goal=goal) if goal else []
            tasks_view = [{
                "id": t.id, "goal": t.child_goal, "state": t.state,
                "expires_at": t.expires_at.isoformat(), "cleanup": t.cleanup_state,
                "external_task": bool(t.external_task_id),
            } for t in tasks]
            outstanding = sum(t.state not in TERMINAL or
                              t.cleanup_state == "CLEANUP_PENDING" for t in tasks)
            rows.append({
                "automation_id": target,
                "title": entry["title"], "enabled_at_import": bool(binding["enabled_at_import"]),
                "mode": binding["integration_mode"], "main_goal": goal,
                "main_goal_hash": binding["main_goal_sha256"],
                "max_children": entry["max_children"], "outstanding_children": outstanding,
                "blocked_by_wip": outstanding >= entry["max_children"] if entry["max_children"] else False,
                "child_tasks": tasks_view,
                "next": "read_only" if entry["mode"] in {"paused", "observe_only"}
                        else "check existing Terminal MCP tasks before new dispatch",
            })
        return {
            "bridge": "projectflow-harness-monitor-v1", "observed_at": now().isoformat(),
            "count": len(rows), "active": sum(bool(x["enabled_at_import"]) for x in rows),
            "paused": sum(not x["enabled_at_import"] for x in rows),
            "monitors": rows,
            "note": "registry is a static automation snapshot; actual ChatGPT scheduling is authoritative",
        }


def decode_mcp(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", False):
        raise HarnessError("terminal provider error")
    obj = getattr(result, "structured_content", None)
    if isinstance(obj, dict) and "status" in obj:
        return obj
    for part in getattr(result, "content", []):
        data = getattr(part, "text", None)
        if isinstance(data, str):
            try:
                decoded = json.loads(data)
            except ValueError:
                continue
            if isinstance(decoded, dict) and "status" in decoded:
                return decoded
    raise HarnessError("no verifiable Terminal MCP response")


async def verify_external_task(task_id: str) -> dict[str, Any]:
    """Ask the authenticated local Terminal MCP, never trust a passed receipt."""
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise HarnessError("Terminal MCP SDK not installed in this Python venv") from exc
    endpoint = os.environ.get("HARNESS_MCP_URL", "http://127.0.0.1:8766/mcp")
    if endpoint != "http://127.0.0.1:8766/mcp":
        raise HarnessError("monitor bridge permits only the local MCP endpoint")
    async with streamable_http_client(endpoint) as streams:
        if hasattr(streams, "read_stream"):
            read, write = streams.read_stream, streams.write_stream
        else:
            read, write = streams[0], streams[1]
        async with ClientSession(read, write) as client:
            await client.initialize()
            response = decode_mcp(await client.call_tool(
                "terminal_task_status", {"task_id": task_id}))
    if response.get("status") != "OK":
        raise HarnessError("external task backend not confirmed")
    details = response.get("result", {})
    actual = details.get("task", {}) if isinstance(details, dict) else {}
    if (not isinstance(actual, dict)
            or str(actual.get("task_id")) != task_id
            or actual.get("mode") not in {"agent", "shell"}
            or actual.get("state") not in
            {"QUEUED", "PENDING", "DISPATCHED", "RUNNING", "WAITING_INPUT",
             "BLOCKED", "DONE", "FAILED", "CANCELLED", "EXPIRED", "IDLE"}):
        raise HarnessError("missing or mismatched real durable task readback")
    return response


def register(
    registry: dict[str, dict[str, Any]], db_path: Path,
    ident: str, *, child_goal: str, dod: str, agent: str,
    node: str, session_name: str, task_id: str,
    ttl_seconds: int, backend: dict[str, Any],
) -> dict[str, Any]:
    if not (0 < ttl_seconds <= 86400):
        raise HarnessError("external child TTL must be between 1 second and 24 hours")
    if not all(isinstance(x, str) and x.strip()
               for x in (child_goal, dod, node, session_name)):
        raise HarnessError("one child goal, DoD, node and session required")
    if agent not in VALID_AGENTS or not TASK_RE.fullmatch(task_id):
        raise HarnessError("agent or durable external task id invalid")
    if backend.get("status") != "OK":
        raise HarnessError("backend receipt not validated")
    task_backend = (backend.get("result") or {}).get("task")
    if (not isinstance(task_backend, dict)
            or task_backend.get("task_id") != task_id
            or task_backend.get("target") != session_name
            or task_backend.get("agent") != agent
            or task_backend.get("mode") not in {"agent", "shell"}):
        raise HarnessError("task receipt target/agent mismatch; refuse registration")
    store = HarnessStore(db_path)
    with connect(db_path) as db:
        init_schema(db)
        entry, binding = require_binding(ident, registry, db)
        if (not entry["configured_enabled"] or entry["mode"] != "observe_and_register"
                or entry["role"] != "parent"):
            raise HarnessError("paused/maintenance monitor cannot dispatch child")
        present = db.execute(
            "SELECT task_id,automation_id FROM harness_monitor_receipts WHERE external_task_id=?",
            (task_id,)).fetchone()
        if present:
            if present["automation_id"] != ident:
                raise HarnessError("receipt already bound to another monitor")
            existing = store.get(present["task_id"])
            if existing.child_goal != child_goal or existing.dod != dod:
                raise HarnessError("conflicting idempotency arguments")
            return {"task_id": existing.id, "state": existing.state, "idempotent": True}
        task = store.create(
            key=f"monitor-{goal_sha(ident + ':' + task_id)}",
            main_goal=binding["main_goal"], child_goal=child_goal,
            dod=dod, agent=agent, node=node, created_at=now(),
            ttl=timedelta(seconds=ttl_seconds))
        task = store.dispatch(task.id, external_task_id=task_id, at=now())
        db.execute("""INSERT INTO harness_monitor_receipts
            (automation_id,task_id,external_task_id,session_name,backend_node,registered_at)
            VALUES (?,?,?,?,?,?)""",
            (ident, task.id, task_id, session_name, node, now().isoformat()))
    return {"task_id": task.id, "state": task.state, "idempotent": False,
            "backend_receipt": task_id, "RUNNING_VERIFIED": False}


async def reconcile_external(
    registry: dict[str, dict[str, Any]], db_path: Path,
) -> dict[str, Any]:
    """Read back real Terminal MCP durable tasks; no implicit acceptance/deletion.

    A FINISHED remote agent still needs project-specific independent DoD.
    UNKNOWN and backend errors never become fake RUNNING or DONE events.
    """
    store = HarnessStore(db_path)
    with connect(db_path) as db:
        init_schema(db)
        rows = db.execute("""SELECT m.automation_id,m.task_id,m.external_task_id,
                                    m.session_name,m.backend_node
                            FROM harness_monitor_receipts m
                            ORDER BY m.task_id""").fetchall()
        pending = [dict(x) for x in rows if store.get(x["task_id"]).state not in TERMINAL]
    changed: list[dict[str, Any]] = []
    for item in pending:
        current = store.get(item["task_id"])
        try:
            entry = registry[item["automation_id"]]
            with connect(db_path) as db:
                require_binding(item["automation_id"], registry, db)
            if not entry["configured_enabled"]:
                raise HarnessError("monitor is paused; no external action allowed")
            result = await verify_external_task(item["external_task_id"])
            backend = result["result"]["task"]
            if (backend.get("target") != item["session_name"]
                    or backend.get("task_id") != item["external_task_id"]):
                raise HarnessError("external task/session mismatch on readback")
            state = backend["state"]
            if state in {"FAILED", "CANCELLED", "EXPIRED"}:
                current = store.fail(
                    current.id, at=now(),
                    reason=f"Terminal MCP {state}; cleanup requires separate proof")
            else:
                backend_time = backend.get("updated_at")
                progress = (datetime.fromtimestamp(float(backend_time), timezone.utc)
                            if isinstance(backend_time, (int, float)) else None)
                live = (state == "RUNNING" and backend.get("running_confirmed") is True
                        and backend.get("turn_state") == "RUNNING"
                        and (backend.get("watcher") or {}).get("alive") is True)
                if state == "DONE":
                    mapped, live = "FINISHED", False
                elif state in {"WAITING_INPUT", "BLOCKED"}:
                    mapped, live = "BLOCKED", False
                elif state == "RUNNING":
                    mapped = "RUNNING"
                else:
                    mapped = "UNKNOWN"
                expected_state = (
                    "RUNNING_VERIFIED" if mapped == "RUNNING" and live
                    else "NOT_RUNNING" if mapped == "FINISHED"
                    else "BLOCKED" if mapped == "BLOCKED" else "UNKNOWN")
                if (current.state != expected_state
                        or current.verified_at is None
                        or now() - current.verified_at >= timedelta(minutes=10)):
                    current = store.observe(
                        current.id, backend_state=mapped,
                        process_alive=True if live else False if mapped == "FINISHED" else None,
                        progress_at=progress if live else None,
                        evidence_ref=f"terminal-mcp-task:{item['external_task_id']}",
                        at=now())
            changed.append({
                "id": current.id, "monitor": item["automation_id"],
                "task_state": current.state, "backend_state": state,
                "cleanup": current.cleanup_state,
                "note": "Remote completion does NOT establish independent DoD",
            })
        except Exception as exc:
            # Any unexpected backend failure retains prior state, even if a
            # remote process might have completed. Expose the error, not PASS.
            changed.append({
                "id": item["task_id"], "monitor": item["automation_id"],
                "task_state": store.get(item["task_id"]).state,
                "backend_state": "UNKNOWN",
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            })
    return {"observed_at": now().isoformat(), "checked": len(pending),
            "results": changed, "unverified_completion_requires_DoD": True}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("bootstrap", "status", "register", "reconcile"))
    p.add_argument("--registry", default=str(REGISTRY))
    p.add_argument("--db", default=str(STATE_DB))
    p.add_argument("--automation-id")
    p.add_argument("--child-goal")
    p.add_argument("--dod")
    p.add_argument("--agent")
    p.add_argument("--node")
    p.add_argument("--session")
    p.add_argument("--task-id")
    p.add_argument("--ttl-seconds", type=int, default=86400)
    args = p.parse_args()
    registry = load_registry(Path(args.registry))
    db_path = Path(args.db).expanduser().resolve()
    if args.action == "bootstrap":
        output = bootstrap(registry, db_path)
    elif args.action == "status":
        output = status(registry, db_path, args.automation_id)
    elif args.action == "reconcile":
        output = asyncio.run(reconcile_external(registry, db_path))
    else:
        if not all((args.automation_id, args.child_goal, args.dod,
                    args.agent, args.node, args.session, args.task_id)):
            p.error("register needs monitor ID, atomic child, DoD, agent, node, session and durable task ID")
        backend = asyncio.run(verify_external_task(args.task_id))
        output = register(
            registry, db_path, args.automation_id,
            child_goal=args.child_goal, dod=args.dod, agent=args.agent,
            node=args.node, session_name=args.session, task_id=args.task_id,
            ttl_seconds=args.ttl_seconds, backend=backend)
    print("HARNESS_MONITOR=" + json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
