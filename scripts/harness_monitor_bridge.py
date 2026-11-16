"""Fail-closed integration of existing ChatGPT monitors with ProjectFlow Harness.

The bridge does not create, enable, reschedule or delete ChatGPT Automations.
It pins the immutable goals of enabled monitors in SQLite, reads child
checkpoints, and can register a Terminal MCP durable task receipt ONLY after
local MCP server readback. It never claims RUNNING or DONE from a receipt.

Do not send passwords, API keys, Zalo data or other private content to this CLI.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterator

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


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Always close the connection after each monitor read/write transaction."""
    if not path.is_file():
        raise HarnessError("Harness DB not installed: do not create a new production DB")
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        with connection:
            yield connection
    finally:
        connection.close()


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
            registered_at TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT 'LOCAL_MCP'
        );
    """)
    cols = {r[1] for r in db.execute("PRAGMA table_info(harness_monitor_receipts)")}
    if "provenance" not in cols:
        db.execute("ALTER TABLE harness_monitor_receipts ADD COLUMN provenance TEXT NOT NULL DEFAULT 'LOCAL_MCP'")


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


# Explicit owner-requested activation: Zalo Hub V2/V229 architecture migration.
# All other paused automations remain read-only inventory.
ZGH_V2_AUTOMATION_ID = "Automation_eec08a5752d08191b3de371065c27320"
ZGH_V2_REPO = "hungtranbkit/zalo-group-hub"


def activate_zgh_v2(
    registry: dict[str, dict[str, Any]], db_path: Path, automation_id: str
) -> dict[str, Any]:
    """One-time, auditable migration of ZGH's paused SQLite monitor binding.

    An explicit automation enable still occurs separately in ChatGPT; this
    function never changes its schedule, owner grants or the S7 runtime.
    """
    if automation_id != ZGH_V2_AUTOMATION_ID:
        raise HarnessError("monitor activation not approved for this automation ID")
    entry = registry.get(automation_id)
    if not entry or not (
        entry.get("configured_enabled") is True
        and entry.get("role") == "parent"
        and entry.get("mode") == "observe_and_register"
        and entry.get("repo") == ZGH_V2_REPO
        and entry.get("max_children") == 2
        and isinstance(entry.get("main_goal"), str)
        and "kiến trúc V2" in entry["main_goal"]
        and "bỏ sót tin nhắn" in entry["main_goal"]
        and "WAL/PIN/grants" in entry["main_goal"]
    ):
        raise HarnessError("reviewed ZGH V2 goal/authority absent or changed")
    goal = entry["main_goal"]
    goal_hash = goal_sha(goal)
    with connect(db_path) as db:
        init_schema(db)
        db.execute("""CREATE TABLE IF NOT EXISTS harness_monitor_activation_events (
            automation_id TEXT PRIMARY KEY,
            previous_mode TEXT NOT NULL,
            target_mode TEXT NOT NULL,
            old_goal_sha256 TEXT NOT NULL,
            new_goal_sha256 TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            authorization TEXT NOT NULL
        )""")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM harness_monitor_bindings WHERE automation_id=?",
            (automation_id,)).fetchone()
        if not row:
            raise HarnessError("ZGH binding absent; bootstrap paused registry first")
        already = (
            row["main_goal"] == goal
            and row["main_goal_sha256"] == goal_hash
            and row["integration_mode"] == "observe_and_register"
            and row["enabled_at_import"] == 1
        )
        if already:
            event = db.execute(
                "SELECT new_goal_sha256 FROM harness_monitor_activation_events "
                "WHERE automation_id=?", (automation_id,)
            ).fetchone()
            if not event or event["new_goal_sha256"] != goal_hash:
                raise HarnessError("active binding lacks matching migration event")
            return {
                "automation_id": automation_id, "state": "ACTIVE_BINDING",
                "already": True, "goal_sha256": goal_hash, "monitor_enabled_by_this_action": False,
            }
        if not (
            row["integration_mode"] == "paused"
            and row["enabled_at_import"] == 0
            and row["main_goal"] is None
            and row["main_goal_sha256"] == goal_sha("")
        ):
            raise HarnessError("GOAL_DRIFT: unexpected pre-activation binding")
        audit_exists = db.execute(
            "SELECT 1 FROM harness_monitor_activation_events WHERE automation_id=?",
            (automation_id,)).fetchone()
        if audit_exists:
            raise HarnessError("activation already recorded against a different binding")
        db.execute("""UPDATE harness_monitor_bindings
            SET main_goal=?,main_goal_sha256=?,integration_mode=?,
                enabled_at_import=?,title=?
            WHERE automation_id=?""",
            (goal, goal_hash, entry["mode"], 1, entry["title"], automation_id))
        db.execute("""INSERT INTO harness_monitor_activation_events (
            automation_id,previous_mode,target_mode,old_goal_sha256,
            new_goal_sha256,activated_at,authorization)
            VALUES (?,?,?,?,?,?,?)""",
            (automation_id, "paused", entry["mode"], goal_sha(""),
             goal_hash, now().isoformat(),
             "owner chat instruction 2026-11-16: enable Harness for Zalo Hub V2 missing-message fix"))
    return {
        "automation_id": automation_id, "state": "ACTIVE_BINDING",
        "already": False, "goal_sha256": goal_hash, "monitor_enabled_by_this_action": False,
    }


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
        # Differentiate managed systemd jobs from Terminal MCP receipts; both
        # carry an external_task_id in HarnessStore, but only the latter is a
        # controller-attested external task.
        linked = {
            int(row["task_id"]): row["provenance"]
            for row in db.execute("SELECT task_id,provenance FROM harness_monitor_receipts")
        }
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        owned = (
            {int(row[0]) for row in db.execute(
                "SELECT task_id FROM harness_managed_units")}
            if "harness_managed_units" in tables else set()
        )
        ids = [ident] if ident else list(registry)
        rows = []
        for target in ids:
            entry, binding = require_binding(target, registry, db)
            goal = binding["main_goal"]
            tasks = store.list_tasks(main_goal=goal) if goal else []
            tasks_view = [{
                "id": t.id, "goal": t.child_goal, "state": t.state,
                "expires_at": t.expires_at.isoformat(), "cleanup": t.cleanup_state,
                "execution_backend": (
                    "MANAGED_SYSTEMD" if t.id in owned else
                    linked.get(t.id, "UNREGISTERED")),
                "external_task": t.id in linked,
                "project_DoD": (
                    "NOT_TESTED_BY_CANARY" if t.child_goal.startswith("CANARY ONLY:")
                    else "INDEPENDENT_ACCEPTANCE_REQUIRED"),
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
                "terminal_turn", {"action": "task", "task_id": task_id,
                                  "compact": True}))
    if response.get("status") != "OK":
        error = (response.get("result") or {}).get("error", "UNKNOWN")
        raise HarnessError(f"external task backend not confirmed: {response.get('status')}/{str(error)[:64]}")
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
    provenance: str = "LOCAL_MCP",
) -> dict[str, Any]:
    if provenance not in {"LOCAL_MCP", "CENTRAL_ATTESTED"}:
        raise HarnessError("unrecognized task receipt provenance")
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
            (automation_id,task_id,external_task_id,session_name,backend_node,registered_at,provenance)
            VALUES (?,?,?,?,?,?,?)""",
            (ident, task.id, task_id, session_name, node, now().isoformat(), provenance))
    return {"task_id": task.id, "state": task.state, "idempotent": False,
            "backend_receipt": task_id, "provenance": provenance,
            "RUNNING_VERIFIED": False}


def observe_attested(
    registry: dict[str, dict[str, Any]], db_path: Path,
    automation_id: str, task_ref: str, backend: dict[str, Any]
) -> dict[str, Any]:
    """Import a trusted caller's central tool readback, NEVER auto-accept DoD.

    This command cannot cryptographically prove the provided JSON came from
    the global Terminal MCP tool. Therefore RUNNING_VERIFIED is disallowed
    here, even for a model-reported RUNNING state.
    """
    store = HarnessStore(db_path)
    with connect(db_path) as db:
        entry, binding = require_binding(automation_id, registry, db)
        row = db.execute("""SELECT * FROM harness_monitor_receipts
                            WHERE automation_id=? AND external_task_id=?""",
                         (automation_id, task_ref)).fetchone()
    if not row or row["provenance"] != "CENTRAL_ATTESTED":
        raise HarnessError("no registered central-attested task")
    remote = (backend.get("result") or {}).get("task", {})
    if (backend.get("status") != "OK" or not isinstance(remote, dict)
            or remote.get("task_id") != task_ref
            or remote.get("target") != row["session_name"]
            or remote.get("state") not in {"RUNNING", "PENDING", "QUEUED",
                "DISPATCHED", "DONE", "FAILED", "BLOCKED", "WAITING_INPUT",
                "CANCELLED", "EXPIRED"}):
        raise HarnessError("controller attestation task/target mismatch")
    task = store.get(row["task_id"])
    state = remote["state"]
    if task.state in TERMINAL:
        return {"task_id": task.id, "state": task.state, "idempotent": True}
    if state in {"FAILED", "CANCELLED", "EXPIRED"}:
        task = store.fail(task.id, at=now(),
                          reason=f"central reported {state}; must reconcile cleanup")
        task = store.cleanup(task.id, at=now(), executor_stopped=False,
                             no_active_refs=False, dirty_worktree=False,
                             evidence_preserved=False, deletion_verified=False)
    else:
        mapped = ("FINISHED" if state == "DONE" else
                  "BLOCKED" if state in {"BLOCKED", "WAITING_INPUT"} else "UNKNOWN")
        task = store.observe(task.id, backend_state=mapped, process_alive=None if
                             mapped != "FINISHED" else False,
                             at=now(), evidence_ref=f"central-attested:{task_ref}")
    return {"task_id": task.id, "state": task.state,
            "backend_reported_state": state,
            "provenance": "CENTRAL_ATTESTED_NOT_CRYPTO_VERIFIED",
            "DoD": "REQUIRES_INDEPENDENT_ACCEPTANCE",
            "cleanup": task.cleanup_state}


def launch_managed_child(
    registry: dict[str, dict[str, Any]], db_path: Path, automation_id: str, *,
    child_goal: str, repo_dir: Path, test_path: str | None = None,
    allowed_files: list[str] | None = None, ttl_seconds: int = 3600,
    executor: str = "codex-task", proof_token: str | None = None,
    workspace_root: Path = Path("/home/dell/workspace"),
    manager_type: Any = None,
) -> dict[str, Any]:
    """Durably dispatch one reviewed coding child under the locked monitor goal.

    With Terminal MCP's durable queue disabled, start can become a send with
    no task_id. The Harness-owned systemd executor remains the durable source.
    Terminal MCP only transports a bounded short CLI command.
    """
    from scripts.harness_managed_exec import ManagedHarness
    import subprocess

    if not isinstance(child_goal, str) or not child_goal.strip():
        raise HarnessError("atomic child goal required")
    if executor == "codex-task":
        if not isinstance(allowed_files, list) or not allowed_files:
            raise HarnessError("explicit source allowlist required")
        if not isinstance(test_path, str) or not test_path.strip():
            raise HarnessError("existing independent pytest required")
    elif executor == "zgh-node-java-qa":
        from scripts.harness_zgh_node_java_qa import RUNNER
        if (automation_id != ZGH_V2_AUTOMATION_ID or test_path != RUNNER
                or allowed_files not in (None, []) or proof_token is not None):
            raise HarnessError("ZGH offline QA is restricted to the exact pinned integration script")
    elif executor == "codex":
        # A read-only agent probe is technical connectivity evidence ONLY.
        # No arbitrary natural-language goal is accepted in this mode.
        if (not child_goal.startswith("CANARY ONLY:")
                or not isinstance(proof_token, str)
                or not re.fullmatch(r"HARNESS_[A-Z0-9_]{12,72}", proof_token)
                or allowed_files not in (None, [])
                or test_path is not None):
            raise HarnessError("read-only canary requires reviewed marker and no code rights")
    else:
        raise HarnessError("unapproved managed executor")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
        raise HarnessError("child TTL must be between 1s and 24h")
    with connect(db_path) as db:
        entry, binding = require_binding(automation_id, registry, db)
        if (entry["role"] != "parent"
                or entry["mode"] != "observe_and_register"
                or not entry["configured_enabled"] or not binding["main_goal"]
                or entry.get("max_children") not in (1, 2)):
            raise HarnessError("paused/maintenance monitor cannot dispatch")
        main_goal = binding["main_goal"]
        expected_remote = entry["repo"]

    # Only a clean project-owned Git clone under Dell workspace is accepted.
    try:
        root = workspace_root.resolve(strict=True)
        selected = repo_dir.resolve(strict=True)
    except OSError as exc:
        raise HarnessError("repo must be inside approved workspace root") from exc
    if root not in selected.parents or selected == root:
        raise HarnessError("repo must be inside approved workspace root")
    if not (selected / ".git").exists():
        raise HarnessError("isolated local Git repository required")
    remote = subprocess.run(
        ["git", "-C", str(selected), "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=10)
    if remote.returncode:
        raise HarnessError("cannot verify project origin")
    raw = remote.stdout.strip()
    prefixes = ("https://github.com/", "git@github.com:", "ssh://git@github.com/")
    matching = [prefix for prefix in prefixes if raw.startswith(prefix)]
    if len(matching) != 1:
        raise HarnessError("origin must use a reviewed github.com remote")
    actual_remote = raw[len(matching[0]):].removesuffix(".git").strip("/")
    if actual_remote != expected_remote:
        raise HarnessError("project origin does not match locked monitor repo")
    current_branch = subprocess.run(
        ["git", "-C", str(selected), "branch", "--show-current"],
        capture_output=True, text=True, timeout=10)
    if current_branch.returncode or not current_branch.stdout.strip().startswith("harness/"):
        raise HarnessError("monitor task requires an isolated harness/* branch")

    impl = manager_type or ManagedHarness
    manager = impl(db_path, db_path.parent)
    receipt = manager.launch(
        main_goal=main_goal, child_goal=child_goal,
        expected=(proof_token if executor == "codex" else test_path),
        repo=str(selected), executor=executor,
        ttl_seconds=ttl_seconds, allowed_files=allowed_files)
    if receipt.get("state") not in {"DISPATCHED", "FAILED", "EXPIRED"}:
        raise HarnessError("executor returned no verifiable state")
    return {
        "automation_id": automation_id,
        "main_goal_hash": goal_sha(main_goal),
        "harness_task_id": receipt["task_id"],
        "state": receipt["state"],
        "deadline": receipt.get("deadline"),
        "unit": receipt.get("unit"),
        "execution_backend": "MANAGED_SYSTEMD",
        "durable_origin": "HARNESS_SQLITE",
        "independent_project_DoD": (
            "NOT_TESTED_BY_AGENT_PROBE" if executor == "codex"
            else "QA_TESTS_ONLY_NOT_DEVICE_E2E" if executor == "zgh-node-java-qa"
            else "NOT_VERIFIED_BY_DISPATCH"),
        "next": "existing systemd Harness reconciler verifies tests, diff and cleanup",
    }


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
                                    m.session_name,m.backend_node,m.provenance
                            FROM harness_monitor_receipts m
                            ORDER BY m.task_id""").fetchall()
        pending = [dict(x) for x in rows if store.get(x["task_id"]).state not in TERMINAL]
    changed: list[dict[str, Any]] = []
    for item in pending:
        current = store.get(item["task_id"])
        if item["provenance"] == "CENTRAL_ATTESTED":
            changed.append({"id": current.id, "monitor": item["automation_id"],
                            "task_state": current.state, "backend_state": "UNAVAILABLE_LOCAL",
                            "note": "central-only task: global Terminal MCP readback required"})
            continue
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
    p.add_argument("action", choices=("bootstrap", "activate-zgh-v2", "status", "launch-managed", "register",
                                      "register-attested", "observe-attested", "reconcile"))
    p.add_argument("--registry", default=str(REGISTRY))
    p.add_argument("--db", default=str(STATE_DB))
    p.add_argument("--automation-id")
    p.add_argument("--child-goal")
    p.add_argument("--dod")
    p.add_argument("--agent")
    p.add_argument("--node")
    p.add_argument("--session")
    p.add_argument("--task-id")
    p.add_argument("--repo-dir", help="clean isolated harness/* Git project clone")
    p.add_argument("--test-path", help="preexisting tracked pytest acceptance relative path")
    p.add_argument("--executor", choices=("codex-task", "codex", "zgh-node-java-qa"),
                   default="codex-task")
    p.add_argument("--proof-token", help="strict read-only Codex connectivity marker")
    p.add_argument("--allowed-file", action="append", default=[],
                   help="one explicitly reviewed tracked source file")
    p.add_argument("--attestation-json", help="Sanitized readback from GLOBAL connected Terminal MCP; not locally verifiable")
    p.add_argument("--ttl-seconds", type=int, default=86400)
    args = p.parse_args()
    registry = load_registry(Path(args.registry))
    db_path = Path(args.db).expanduser().resolve()
    if args.action == "bootstrap":
        output = bootstrap(registry, db_path)
    elif args.action == "activate-zgh-v2":
        if not args.automation_id:
            p.error("activate-zgh-v2 requires reviewed Zalo Hub automation ID")
        output = activate_zgh_v2(registry, db_path, args.automation_id)
    elif args.action == "status":
        output = status(registry, db_path, args.automation_id)
    elif args.action == "reconcile":
        output = asyncio.run(reconcile_external(registry, db_path))
    elif args.action == "launch-managed":
        if not all((args.automation_id, args.child_goal, args.repo_dir)):
            p.error("launch-managed requires monitor ID, atomic child and isolated repo")
        output = launch_managed_child(
            registry, db_path, args.automation_id,
            child_goal=args.child_goal, repo_dir=Path(args.repo_dir),
            test_path=args.test_path, allowed_files=args.allowed_file,
            executor=args.executor, proof_token=args.proof_token,
            ttl_seconds=args.ttl_seconds)
    elif args.action == "observe-attested":
        if not all((args.automation_id, args.task_id, args.attestation_json)):
            p.error("observe-attested needs monitor id, durable task id and central tool readback")
        output = observe_attested(registry, db_path, args.automation_id,
                                  args.task_id, json.loads(args.attestation_json))
    else:
        if not all((args.automation_id, args.child_goal, args.dod,
                    args.agent, args.node, args.session, args.task_id)):
            p.error("register needs monitor ID, atomic child, DoD, agent, node, session and durable task ID")
        backend = (asyncio.run(verify_external_task(args.task_id))
                   if args.action == "register" else
                   json.loads(args.attestation_json or "{}"))
        output = register(
            registry, db_path, args.automation_id,
            child_goal=args.child_goal, dod=args.dod, agent=args.agent,
            node=args.node, session_name=args.session, task_id=args.task_id,
            ttl_seconds=args.ttl_seconds, backend=backend,
            provenance=("LOCAL_MCP" if args.action == "register" else "CENTRAL_ATTESTED"))
    print("HARNESS_MONITOR=" + json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
