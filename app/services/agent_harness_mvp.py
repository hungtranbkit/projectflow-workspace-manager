"""Fail-closed, SQLite-backed one-task harness state machine.

This is a *non-production* MVP. It records evidence supplied by a trusted
executor adapter; it does not run, stop, delete, or grant access to anything.
An adapter to Terminal MCP must be separately live-tested before activation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator

MAX_TTL = timedelta(hours=24)
MAX_ACTIVE_CHILDREN = 2
FRESH_WINDOW = timedelta(minutes=60)
TERMINAL = frozenset({"DONE_VERIFIED", "FAILED", "EXPIRED"})


class HarnessError(ValueError):
    """Refuse unsafe transitions or unverifiable state."""


def _dt(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HarnessError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _dt(value).isoformat()


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Task:
    id: int
    key: str
    main_goal: str
    child_goal: str
    dod: str
    agent: str
    node: str
    state: str
    created_at: datetime
    expires_at: datetime
    external_task_id: str | None
    verified_at: datetime | None
    cleanup_state: str


class HarnessStore:
    """One atomic goal per durable task. All writes are transactional."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback AND close SQLite fds deterministically.

        sqlite3.Connection.__exit__ commits, but does NOT close connections.
        Long-running monitor loops leaked ~2 descriptors per read before this.
        """
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout = 10000")
            with db:
                yield db
        finally:
            db.close()

    def _init(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS harness_tasks (
                    id INTEGER PRIMARY KEY,
                    task_key TEXT NOT NULL UNIQUE,
                    main_goal TEXT NOT NULL,
                    child_goal TEXT NOT NULL,
                    dod TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    node TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    external_task_id TEXT,
                    verified_at TEXT,
                    cleanup_state TEXT NOT NULL DEFAULT 'NOT_STARTED'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_harness_active_goal
                    ON harness_tasks(main_goal, child_goal)
                    WHERE state NOT IN ('DONE_VERIFIED','FAILED','EXPIRED');
                CREATE TABLE IF NOT EXISTS harness_events (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES harness_tasks(id),
                    at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                );
            """)

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], key=row["task_key"], main_goal=row["main_goal"],
            child_goal=row["child_goal"], dod=row["dod"], agent=row["agent"],
            node=row["node"], state=row["state"],
            created_at=_from_iso(row["created_at"]), expires_at=_from_iso(row["expires_at"]),
            external_task_id=row["external_task_id"],
            verified_at=_from_iso(row["verified_at"]) if row["verified_at"] else None,
            cleanup_state=row["cleanup_state"],
        )

    def get(self, task_id: int) -> Task:
        with self._connect() as db:
            row = db.execute("SELECT * FROM harness_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HarnessError("task not found")
        return self._task(row)

    def events(self, task_id: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT at,event,evidence_json FROM harness_events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [{"at": row["at"], "event": row["event"], "evidence": json.loads(row["evidence_json"])} for row in rows]

    @staticmethod
    def _event(db: sqlite3.Connection, task_id: int, at: datetime, name: str, evidence: dict[str, Any]) -> None:
        db.execute("INSERT INTO harness_events(task_id,at,event,evidence_json) VALUES (?,?,?,?)",
                   (task_id, _iso(at), name, json.dumps(evidence, sort_keys=True, ensure_ascii=False)))

    def create(self, *, key: str, main_goal: str, child_goal: str, dod: str, agent: str,
               node: str, created_at: datetime, ttl: timedelta = MAX_TTL) -> Task:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task_id = self._create_in_transaction(db, key=key, main_goal=main_goal,
                child_goal=child_goal, dod=dod, agent=agent, node=node, created_at=created_at, ttl=ttl)
        return self.get(task_id)

    def _create_in_transaction(self, db: sqlite3.Connection, *, key: str, main_goal: str, child_goal: str, dod: str, agent: str, node: str, created_at: datetime, ttl: timedelta) -> int:
        if not db.in_transaction:
            raise HarnessError('active transaction required')
        start = _dt(created_at)
        if not (timedelta(0) < ttl <= MAX_TTL):
            raise HarnessError("child TTL must be >0 and <=24 hours")
        if not all(isinstance(v, str) and v.strip() for v in (key, main_goal, child_goal, dod, agent, node)):
            raise HarnessError("all task fields are required")
        # One active task for exactly the same main/child goal; not a hidden
        # way to spawn duplicates with new request keys.
        dup = db.execute("""SELECT id,state,cleanup_state FROM harness_tasks
            WHERE main_goal=? AND child_goal=? ORDER BY id DESC LIMIT 1""",
            (main_goal, child_goal)).fetchone()
        if dup is not None:
            if dup["state"] not in TERMINAL or dup["cleanup_state"] != "CLEANED_VERIFIED":
                raise HarnessError(f"duplicate active child task {dup['id']}")
            raise HarnessError(
                f"previously attempted child task {dup['id']}; rescope into a new atomic goal")
        # A completed/expired child with unverified cleanup still owns
        # resources. Do not create a third worker just because a previous
        # agent reported DONE or hit its TTL.
        outstanding = db.execute("""SELECT COUNT(*) FROM harness_tasks
            WHERE main_goal=? AND
            (state NOT IN ('DONE_VERIFIED','FAILED','EXPIRED')
             OR cleanup_state='CLEANUP_PENDING')""", (main_goal,)).fetchone()[0]
        if outstanding >= MAX_ACTIVE_CHILDREN:
            raise HarnessError("parent WIP limit: two outstanding child tasks")
        try:
            cur = db.execute("""INSERT INTO harness_tasks
                (task_key,main_goal,child_goal,dod,agent,node,state,created_at,expires_at)
                VALUES (?,?,?,?,?,?, 'NOT_DISPATCHED',?,?)""",
                (key, main_goal, child_goal, dod, agent, node, _iso(start), _iso(start + ttl)))
        except sqlite3.IntegrityError as exc:
            raise HarnessError("duplicate idempotency key") from exc
        self._event(db, cur.lastrowid, start, "CREATED", {"expires_at": _iso(start + ttl)})
        task_id = cur.lastrowid
        return task_id

    def adopt_in_transaction(self, db: sqlite3.Connection, *, key: str,
                             main_goal: str, child_goal: str, dod: str, agent: str,
                             node: str, at: datetime, ttl: timedelta,
                             external_task_id: str) -> Task:
        """Bind an existing external execution, without launching anything.
        Caller owns the same immediate transaction as the monitor receipt.
        """
        if not db.in_transaction or not external_task_id.strip():
            raise HarnessError("adoption requires an active transaction and durable ID")
        if db.execute("SELECT 1 FROM harness_tasks WHERE external_task_id=?",
                      (external_task_id,)).fetchone():
            raise HarnessError("external task already recorded; reconcile existing owner")
        task_id = self._create_in_transaction(db, key=key, main_goal=main_goal,
            child_goal=child_goal, dod=dod, agent=agent, node=node, created_at=at, ttl=ttl)
        self._record_dispatch(db, task_id, external_task_id=external_task_id, at=at)
        return self._task(db.execute("SELECT * FROM harness_tasks WHERE id=?", (task_id,)).fetchone())

    def list_tasks(self, *, main_goal: str | None = None) -> list[Task]:
        """Current durable task snapshot for a monitor parent, not live agent proof."""
        with self._connect() as db:
            if main_goal is None:
                rows = db.execute("SELECT * FROM harness_tasks ORDER BY id").fetchall()
            else:
                rows = db.execute("SELECT * FROM harness_tasks WHERE main_goal=? ORDER BY id",
                                  (main_goal,)).fetchall()
        return [self._task(row) for row in rows]

    def reconcile_due(self, *, at: datetime) -> list[Task]:
        """Persist all overdue tasks; never pretend that expiring kills a process.

        A separate authorized supervisor must verify cancellation and cleanup.
        Idempotent even if two scheduler ticks race on the same SQLite DB.
        """
        now = _dt(at)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT id FROM harness_tasks
                WHERE state NOT IN ('DONE_VERIFIED','FAILED','EXPIRED') AND expires_at<=?
                ORDER BY id""", (_iso(now),)).fetchall()
            ids = [int(row["id"]) for row in rows]
            for task_id in ids:
                db.execute("""UPDATE harness_tasks
                    SET state='EXPIRED', verified_at=?, cleanup_state='CLEANUP_PENDING'
                    WHERE id=?""", (_iso(now), task_id))
                self._event(db, task_id, now, "EXPIRED", {
                    "reason": "deadline reached; process cancellation NOT verified",
                    "cleanup": "CLEANUP_PENDING"})
        return [self.get(i) for i in ids]

    def _expire_if_due(self, task_id: int, now: datetime) -> None:
        # Make expiry durable in its own committed transaction: a subsequent
        # refused mutation must not roll it back.
        with self._connect() as db:
            row = db.execute("SELECT * FROM harness_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise HarnessError("task not found")
            if row["state"] not in TERMINAL and _dt(now) >= _from_iso(row["expires_at"]):
                db.execute("""UPDATE harness_tasks SET
                    state='EXPIRED', verified_at=?, cleanup_state='CLEANUP_PENDING'
                    WHERE id=?""", (_iso(now), task_id))
                self._event(db, task_id, now, "EXPIRED", {
                    "reason": "TTL reached; external cancel still needs independent confirmation"})

    @staticmethod
    def _guard(db: sqlite3.Connection, task_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM harness_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HarnessError("task not found")
        if row["state"] in TERMINAL:
            raise HarnessError(f"task terminal: {row['state']}")
        return row

    def dispatch(self, task_id: int, *, external_task_id: str, at: datetime) -> Task:
        now = _dt(at)
        if not external_task_id or not external_task_id.strip():
            raise HarnessError("durable task receipt id required")
        self._expire_if_due(task_id, now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._record_dispatch(db, task_id, external_task_id=external_task_id, at=now)
        return self.get(task_id)

    def _record_dispatch(self, db: sqlite3.Connection, task_id: int, *,
                         external_task_id: str, at: datetime) -> None:
        now = _dt(at)
        row = self._guard(db, task_id)
        if row["state"] != "NOT_DISPATCHED":
            raise HarnessError("dispatch already recorded")
        db.execute("UPDATE harness_tasks SET state='DISPATCHED', external_task_id=?, verified_at=? WHERE id=?",
                   (external_task_id, _iso(now), task_id))
        self._event(db, task_id, now, "DISPATCHED", {"external_task_id": external_task_id, "running": False})

    def observe(self, task_id: int, *, backend_state: str, process_alive: bool | None,
                at: datetime, progress_at: datetime | None = None, evidence_ref: str | None = None) -> Task:
        now = _dt(at)
        progress = _dt(progress_at) if progress_at is not None else None
        self._expire_if_due(task_id, now)
        if self.get(task_id).state == "EXPIRED":
            return self.get(task_id)
        with self._connect() as db:
            row = self._guard(db, task_id)
            if not row["external_task_id"]:
                raise HarnessError("cannot observe execution before dispatch receipt")
            if (backend_state == "RUNNING" and process_alive is True and progress is not None
                    and timedelta(0) <= now - progress <= FRESH_WINDOW and evidence_ref):
                state = "RUNNING_VERIFIED"
            elif backend_state in {"IDLE", "FINISHED", "EXITED", "NOT_FOUND"} or process_alive is False:
                state = "NOT_RUNNING"
            elif backend_state in {"WAITING_INPUT", "BLOCKED"}:
                state = "BLOCKED"
            elif backend_state == "RUNNING" and process_alive is True:
                state = "STALLED"  # running process != recent useful progress
            else:
                state = "UNKNOWN"
            db.execute("UPDATE harness_tasks SET state=?,verified_at=? WHERE id=?",
                       (state, _iso(now), task_id))
            self._event(db, task_id, now, "OBSERVED", {"state": state, "backend_state": backend_state,
                         "process_alive": process_alive, "progress_at": _iso(progress) if progress else None,
                         "evidence_ref": evidence_ref})
        return self.get(task_id)

    def accept(self, task_id: int, *, at: datetime, artifact_ref: str | None,
               test_evidence: str | None, runtime_readback: str | None,
               verifier: Callable[[str, str, str], bool] | None = None) -> Task:
        now = _dt(at)
        self._expire_if_due(task_id, now)
        with self._connect() as db:
            row = self._guard(db, task_id)
            if row["state"] not in {"RUNNING_VERIFIED", "NOT_RUNNING", "STALLED"}:
                raise HarnessError("acceptance requires observed execution, not a dispatch receipt")
            # Refs must identify actual evidence verified independently by the
            # caller. Blank/missing proof is refused without inventing PASS.
            if not all(isinstance(v, str) and v.strip() for v in (artifact_ref, test_evidence, runtime_readback)):
                raise HarnessError("artifact, test and runtime evidence required")
            # No trusted verifier => no PASS. Production adapters must implement
            # independent artifact + test-exit + runtime readback checks.
            if verifier is None or not verifier(artifact_ref, test_evidence, runtime_readback):
                raise HarnessError("independent acceptance verifier missing or failed")
            db.execute("""UPDATE harness_tasks SET
                state='DONE_VERIFIED',verified_at=?,cleanup_state='CLEANUP_PENDING'
                WHERE id=?""", (_iso(now), task_id))
            self._event(db, task_id, now, "DONE_VERIFIED", {"artifact": artifact_ref,
                         "test": test_evidence, "runtime": runtime_readback})
        return self.get(task_id)

    def fail(self, task_id: int, *, at: datetime, reason: str) -> Task:
        now = _dt(at)
        if not reason.strip():
            raise HarnessError("failure reason required")
        self._expire_if_due(task_id, now)
        if self.get(task_id).state == "EXPIRED":
            return self.get(task_id)
        with self._connect() as db:
            row = self._guard(db, task_id)
            db.execute("""UPDATE harness_tasks SET
                state='FAILED',verified_at=?,cleanup_state='CLEANUP_PENDING'
                WHERE id=?""", (_iso(now), task_id))
            self._event(db, task_id, now, "FAILED", {"reason": reason})
        return self.get(task_id)

    def expire(self, task_id: int, *, at: datetime) -> Task:
        now = _dt(at)
        self._expire_if_due(task_id, now)
        return self.get(task_id)

    def cleanup(self, task_id: int, *, at: datetime, executor_stopped: bool, no_active_refs: bool,
                dirty_worktree: bool, evidence_preserved: bool, deletion_verified: bool) -> Task:
        now = _dt(at)
        with self._connect() as db:
            row = db.execute("SELECT * FROM harness_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise HarnessError("task not found")
            if row["state"] not in TERMINAL:
                raise HarnessError("cannot cleanup unfinished task")
            safe = executor_stopped and no_active_refs and not dirty_worktree and evidence_preserved and deletion_verified
            state = "CLEANED_VERIFIED" if safe else "CLEANUP_PENDING"
            db.execute("UPDATE harness_tasks SET cleanup_state=? WHERE id=?", (state, task_id))
            self._event(db, task_id, now, state, {"executor_stopped": executor_stopped,
                "no_active_refs": no_active_refs, "dirty_worktree": dirty_worktree,
                "evidence_preserved": evidence_preserved, "deletion_verified": deletion_verified})
        return self.get(task_id)
