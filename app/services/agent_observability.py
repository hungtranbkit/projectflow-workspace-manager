"""Agent observability P0 (specs/001-agent-observability-recovery, T005-T009).

DRAFT, read-only, additive. Nothing in this module starts, stops, retries or
schedules an agent; nothing here writes to Task/Workspace/AgentSession tables
or the Harness task ledger. It is not wired into any execution path and does
not claim SpecGate approval (the machine-readable FeatureSpec, T004, is still
pending review).

Layers:
- normalize(): untrusted raw dict -> NormalizedEvent | Quarantine. Allowlisted
  schema, identity validation, provenance capped by source, secret redaction.
- ObservabilityEventStore: bounded, namespaced SQLite event ledger with a
  durable dedupe ledger (replay-safe), session identity bindings (no
  relabeling) and a quarantine table. Ingest order is monotonic (seq);
  occurred_at is display time only.
- derive_task_view(): three INDEPENDENT dimensions (dispatch / execution /
  delivery milestones) + freshness. Event volume never changes completion.
- attach_harness(): completion % and child TTL are copied verbatim from the
  existing Harness progress snapshot (scripts/harness_progress.summarize);
  this module never computes its own percentage.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
import base64
import binascii
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

# Provenance, weakest -> strongest. Order matters (index = strength).
PROVENANCE_LEVELS = (
    "SELF_REPORTED",          # agent's own claim (WORK_STATUS=READY, chat text)
    "CENTRAL_ATTESTED",       # forwarded Terminal MCP readback, NOT crypto proof
    "LOCAL_OBSERVED",         # ProjectFlow/Harness saw it locally (PTY, systemd)
    "LOCAL_VERIFIED",         # real evidence row (test_runs/review_runs/qa_runs/merge/deploy)
    "INDEPENDENT_ACCEPTANCE", # independent acceptance verifier / reviewed gate
)
_RANK = {p: i for i, p in enumerate(PROVENANCE_LEVELS)}

# Each source has a hard provenance ceiling. A raw event that claims more
# than its source can prove is downgraded and flagged (spoofed provenance).
SOURCE_CEILING = {
    "agent_self_report": "SELF_REPORTED",
    "cli_hook": "LOCAL_OBSERVED",
    "terminal_mcp_central": "CENTRAL_ATTESTED",
    "terminal_mcp_local": "LOCAL_OBSERVED",
    "projectflow_agent_session": "LOCAL_OBSERVED",
    "projectflow_evidence": "LOCAL_VERIFIED",
    "harness_store": "LOCAL_OBSERVED",
    "harness_acceptance": "INDEPENDENT_ACCEPTANCE",
}

DISPATCH_KINDS = {"task_queued", "prompt_submitted", "session_started"}
LIVENESS_KINDS = {"session_started", "heartbeat"}
PROGRESS_KINDS = {"tool_call", "output_growth"}
EXEC_TERMINAL_KINDS = {"process_exited", "process_failed"}
EXEC_KINDS = (LIVENESS_KINDS | PROGRESS_KINDS | EXEC_TERMINAL_KINDS
              | {"waiting_input", "node_disconnected", "status_unknown"})
# Delivery milestones in pipeline order. Each requires its own evidence.
MILESTONE_KINDS = {
    "test_result": ("tests_passed", "LOCAL_VERIFIED"),
    "review_result": ("review_passed", "LOCAL_VERIFIED"),
    "qa_result": ("qa_passed", "LOCAL_VERIFIED"),
    "merge_recorded": ("merged", "LOCAL_VERIFIED"),
    "deploy_recorded": ("deployed", "LOCAL_VERIFIED"),
    "acceptance_recorded": ("accepted", "INDEPENDENT_ACCEPTANCE"),
}
KINDS = DISPATCH_KINDS | EXEC_KINDS | set(MILESTONE_KINDS)
RESULTS = {"PASS", "FAIL", "PENDING", "UNKNOWN", "ERROR"}
TOOL_KINDS = {"read", "edit", "write", "bash", "test", "git", "task", "search", "other"}

DISPATCH_STATES = ("QUEUED", "DISPATCHED_UNVERIFIED", "ACKNOWLEDGED")
EXECUTION_STATES = ("RUNNING_VERIFIED", "WAITING_FOR_INPUT", "STALLED",
                    "EXITED", "FAILED", "UNKNOWN")
DELIVERY_STAGES = ("NOT_STARTED", "CODING", "TESTING", "REVIEW", "QA",
                   "MERGED", "DEPLOYED", "ACCEPTED")

REQUIRED = ("event_id", "schema_version", "source", "occurred_at", "node_id",
            "project_id", "task_id", "session_id", "agent_id", "kind",
            "provenance_level")
OPTIONAL = ("parent_agent_id", "worktree_ref", "branch_ref",
            "sanitized_evidence_ref", "result", "tool_kind", "dedupe_key",
            "observed_at", "evidence_currency")
IDENTITY = ("node_id", "project_id", "task_id", "session_id", "agent_id")
# Whether a delivery-evidence event proves the CURRENT commit/brief. Only
# CURRENT (or absent, for adapters that only ever emit current evidence)
# may set milestones.*; HISTORIC/UNKNOWN rows are shown as history only.
EVIDENCE_CURRENCY = ("CURRENT", "HISTORIC", "UNKNOWN")

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
REF_RE = re.compile(r"^[a-z_]{2,40}:[A-Za-z0-9_.:/-]{1,128}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")

# Values that look like credentials or private locations are never stored.
SECRET_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"gh[pousr]_[A-Za-z0-9]{16,}", r"github_pat_[A-Za-z0-9_]{20,}",
        r"\bsk-[A-Za-z0-9_-]{16,}", r"\bxox[abpr]-[A-Za-z0-9-]{10,}",
        r"\bAKIA[0-9A-Z]{16}\b", r"-----BEGIN [A-Z ]*PRIVATE KEY",
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JWT
        r"(password|passwd|secret|token|api[_-]?key|authorization|bearer)\s*[:=]",
        r"\bbearer\s+[A-Za-z0-9._-]{8,}",
    )
]
PRIVATE_LOCATION = [
    re.compile(r"(^|[\s:=])/(home|root|Users|etc|var|tmp)/"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
]


class ObservabilityError(ValueError):
    pass


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        obj = value
    elif isinstance(value, str):
        obj = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ObservabilityError("timestamp must be ISO-8601 string")
    if obj.tzinfo is None or obj.utcoffset() is None:
        raise ObservabilityError("timestamp must be timezone-aware")
    return obj.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def looks_sensitive(value: str) -> bool:
    return any(p.search(value) for p in SECRET_PATTERNS)


def looks_private_location(value: str) -> bool:
    return any(p.search(value) for p in PRIVATE_LOCATION)


# --------------------------------------------------------------------------
# T005 — normalized event schema + provenance classification
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    schema_version: int
    source: str
    occurred_at: str
    observed_at: str
    node_id: str
    project_id: str
    task_id: str
    session_id: str
    agent_id: str
    kind: str
    provenance_level: str
    dedupe_key: str
    parent_agent_id: str | None = None
    worktree_ref: str | None = None
    branch_ref: str | None = None
    sanitized_evidence_ref: str | None = None
    result: str | None = None
    tool_kind: str | None = None
    redaction_flags: tuple[str, ...] = ()
    evidence_currency: str | None = None

    def content_hash(self) -> str:
        body = {k: v for k, v in asdict(self).items() if k not in ("observed_at",)}
        if body.get("evidence_currency") is None:  # keep pre-existing hashes stable
            body.pop("evidence_currency", None)
        body["redaction_flags"] = list(body["redaction_flags"])
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def to_public(self) -> dict[str, Any]:
        """Operator read model. Still private (operator-only); no raw payload
        exists anywhere in this object to leak."""
        out = asdict(self)
        out["redaction_flags"] = list(self.redaction_flags)
        return out


@dataclass(frozen=True)
class Quarantine:
    reason: str
    detail: str
    event_id: str | None
    source: str | None
    redaction_flags: tuple[str, ...] = field(default=())


def _clean_field(name: str, value: Any, flags: list[str]) -> str | None:
    """Return a safe string or None (dropped with a flag). Never echoes the
    rejected value into the flag."""
    if value is None:
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        flags.append(f"invalid_type:{name}")
        return None
    text = str(value)
    if looks_sensitive(text):
        flags.append(f"secret_redacted:{name}")
        return None
    if looks_private_location(text):
        flags.append(f"private_location_redacted:{name}")
        return None
    return text


def normalize(raw: Any, *, observed_at: datetime | None = None) -> NormalizedEvent | Quarantine:
    """Validate one untrusted raw event. Missing/invalid identity is
    QUARANTINED, never guessed from filenames/terminal titles."""
    if not isinstance(raw, dict):
        return Quarantine("malformed", "event is not an object", None, None)
    flags: list[str] = []
    allowed = set(REQUIRED) | set(OPTIONAL)
    for key in sorted(raw):
        if key not in allowed:
            safe_key = key if isinstance(key, str) and ID_RE.match(key) else "unnamed"
            flags.append(f"dropped_field:{safe_key}")
    event_id = raw.get("event_id")
    source = raw.get("source") if raw.get("source") in SOURCE_CEILING else None
    eid = event_id if isinstance(event_id, str) and ID_RE.match(event_id) and not looks_sensitive(event_id) else None

    def q(reason: str, detail: str) -> Quarantine:
        return Quarantine(reason, detail, eid, source, tuple(flags))

    if raw.get("schema_version") != SCHEMA_VERSION:
        return q("schema_version", "unsupported schema_version")
    if source is None:
        return q("unknown_source", "source is not a registered adapter")
    if eid is None:
        return q("invalid_event_id", "event_id missing or invalid")
    ident: dict[str, str] = {}
    for name in IDENTITY:
        value = _clean_field(name, raw.get(name), flags)
        if value is None or not ID_RE.match(value):
            return q("missing_identity", f"{name} missing or invalid")
        ident[name] = value
    kind = raw.get("kind")
    if kind not in KINDS:
        return q("unknown_kind", "kind not in schema")
    claimed = raw.get("provenance_level")
    if claimed not in _RANK:
        return q("invalid_provenance", "provenance_level not in schema")
    ceiling = SOURCE_CEILING[source]
    provenance = claimed
    if _RANK[claimed] > _RANK[ceiling]:
        provenance = ceiling
        flags.append("provenance_downgraded")
    try:
        occurred = _aware(raw.get("occurred_at"))
    except (ObservabilityError, ValueError):
        return q("invalid_timestamp", "occurred_at missing or naive")
    observed = _aware(observed_at or datetime.now(timezone.utc))
    if occurred - observed > timedelta(minutes=5):
        return q("future_timestamp", "occurred_at too far after observed_at")

    opt: dict[str, Any] = {}
    for name in ("parent_agent_id",):
        value = _clean_field(name, raw.get(name), flags)
        opt[name] = value if value and ID_RE.match(value) else None
    branch = _clean_field("branch_ref", raw.get("branch_ref"), flags)
    opt["branch_ref"] = branch if branch and BRANCH_RE.match(branch) else None
    # worktree_ref is an opaque id, never a filesystem path.
    wt = _clean_field("worktree_ref", raw.get("worktree_ref"), flags)
    if wt and ("/" in wt or "\\" in wt):
        flags.append("private_location_redacted:worktree_ref")
        wt = None
    opt["worktree_ref"] = wt if wt and ID_RE.match(wt) else None
    ref = _clean_field("sanitized_evidence_ref", raw.get("sanitized_evidence_ref"), flags)
    if ref and not REF_RE.match(ref):
        flags.append("invalid_evidence_ref")
        ref = None
    opt["sanitized_evidence_ref"] = ref
    result = raw.get("result")
    opt["result"] = result if result in RESULTS else None
    if result is not None and opt["result"] is None:
        flags.append("invalid_result")
    currency = raw.get("evidence_currency")
    opt["evidence_currency"] = currency if currency in EVIDENCE_CURRENCY else None
    if currency is not None and opt["evidence_currency"] is None:
        flags.append("invalid_evidence_currency")  # never echo the value
        opt["evidence_currency"] = "UNKNOWN"
    tool = raw.get("tool_kind")
    opt["tool_kind"] = tool if tool in TOOL_KINDS else ("other" if tool is not None else None)

    dedupe = raw.get("dedupe_key")
    if not (isinstance(dedupe, str) and ID_RE.match(dedupe) and not looks_sensitive(dedupe)):
        if dedupe is not None:
            flags.append("invalid_dedupe_key")
        dedupe = hashlib.sha256(f"{source}|{eid}".encode()).hexdigest()[:40]
    return NormalizedEvent(
        event_id=eid, schema_version=SCHEMA_VERSION, source=source,
        occurred_at=_iso(occurred), observed_at=_iso(observed),
        kind=kind, provenance_level=provenance, dedupe_key=dedupe,
        redaction_flags=tuple(flags), **ident, **opt)


# --------------------------------------------------------------------------
# T006/T007 — replay-safe, bounded event store (namespaced SQLite tables)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestResult:
    status: str  # ACCEPTED | DUPLICATE | QUARANTINED
    reason: str | None = None
    seq: int | None = None
    late: bool = False


class ObservabilityEventStore:
    """Namespaced `agent_obs_*` tables. Intended to live inside the existing
    Harness SQLite file (same persistence boundary, no second task DB); it
    never reads or writes harness_tasks / harness_events."""

    def __init__(self, db_path: str | Path, *, max_events: int = 50_000,
                 max_quarantine: int = 5_000, max_dedupe: int = 500_000) -> None:
        if max_events < 1 or max_quarantine < 1 or max_dedupe < max_events:
            raise ObservabilityError("invalid retention bounds")
        self.path = str(db_path)
        self.max_events = max_events
        self.max_quarantine = max_quarantine
        self.max_dedupe = max_dedupe
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
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
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_obs_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    content_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    late INTEGER NOT NULL DEFAULT 0,
                    event_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_obs_task
                    ON agent_obs_events(task_id, seq);
                CREATE TABLE IF NOT EXISTS agent_obs_dedupe (
                    dedupe_key TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    first_seq INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_obs_event_ids (
                    source TEXT NOT NULL, event_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL, content_hash TEXT NOT NULL,
                    first_seq INTEGER NOT NULL,
                    PRIMARY KEY (source, event_id)
                );
                CREATE TABLE IF NOT EXISTS agent_obs_bindings (
                    session_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    first_event_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_obs_quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL,
                    event_id TEXT, source TEXT, flags_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_obs_counters (
                    name TEXT PRIMARY KEY, value INTEGER NOT NULL
                );
            """)

    @staticmethod
    def _bump(db: sqlite3.Connection, name: str) -> None:
        db.execute("""INSERT INTO agent_obs_counters(name,value) VALUES(?,1)
                      ON CONFLICT(name) DO UPDATE SET value=value+1""", (name,))

    def _quarantine(self, db: sqlite3.Connection, q: Quarantine, at: datetime) -> None:
        db.execute("""INSERT INTO agent_obs_quarantine(at,reason,detail,event_id,source,flags_json)
                      VALUES(?,?,?,?,?,?)""",
                   (_iso(at), q.reason, q.detail, q.event_id, q.source,
                    json.dumps(list(q.redaction_flags))))
        self._bump(db, "quarantined")
        db.execute("""DELETE FROM agent_obs_quarantine WHERE id <=
                      (SELECT MAX(id) FROM agent_obs_quarantine) - ?""", (self.max_quarantine,))

    def ingest(self, raw: Any, *, observed_at: datetime | None = None) -> IngestResult:
        at = _aware(observed_at or datetime.now(timezone.utc))
        item = normalize(raw, observed_at=at)
        with self._connect() as db:
            if isinstance(item, Quarantine):
                self._quarantine(db, item, at)
                return IngestResult("QUARANTINED", item.reason)
            chash = item.content_hash()
            prior = db.execute("SELECT content_hash FROM agent_obs_dedupe WHERE dedupe_key=?",
                               (item.dedupe_key,)).fetchone()
            if prior is not None:
                if prior["content_hash"] == chash:
                    self._bump(db, "duplicates")
                    return IngestResult("DUPLICATE")
                # Same identity, different content: never rewrite history.
                self._quarantine(db, Quarantine("replay_conflict",
                                 "dedupe_key reused with different content",
                                 item.event_id, item.source, item.redaction_flags), at)
                return IngestResult("QUARANTINED", "replay_conflict")
            # A stable event_id is bound to exactly one dedupe_key + content
            # per source: a reused id with another key/data is quarantined,
            # never stored as a second, reassigned copy of "the same" event.
            owner = db.execute("""SELECT dedupe_key FROM agent_obs_event_ids
                                  WHERE source=? AND event_id=?""",
                               (item.source, item.event_id)).fetchone()
            if owner is not None:
                self._quarantine(db, Quarantine("event_id_conflict",
                                 "event_id already bound to another dedupe_key/content",
                                 item.event_id, item.source, item.redaction_flags), at)
                return IngestResult("QUARANTINED", "event_id_conflict")
            bound = db.execute("SELECT * FROM agent_obs_bindings WHERE session_id=?",
                               (item.session_id,)).fetchone()
            if bound is None:
                db.execute("""INSERT INTO agent_obs_bindings
                              (session_id,node_id,project_id,task_id,agent_id,first_event_id)
                              VALUES(?,?,?,?,?,?)""",
                           (item.session_id, item.node_id, item.project_id,
                            item.task_id, item.agent_id, item.event_id))
            elif any(bound[k] != getattr(item, k) for k in
                     ("node_id", "project_id", "task_id", "agent_id")):
                self._quarantine(db, Quarantine("identity_conflict",
                                 "session already bound to another node/project/task/agent",
                                 item.event_id, item.source, item.redaction_flags), at)
                return IngestResult("QUARANTINED", "identity_conflict")
            newest = db.execute("""SELECT MAX(occurred_at) FROM agent_obs_events
                                   WHERE session_id=?""", (item.session_id,)).fetchone()[0]
            late = bool(newest and _aware(newest) > _aware(item.occurred_at))
            cur = db.execute("""INSERT INTO agent_obs_events
                (dedupe_key,content_hash,task_id,session_id,occurred_at,observed_at,late,event_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (item.dedupe_key, chash, item.task_id, item.session_id,
                 item.occurred_at, item.observed_at, int(late),
                 json.dumps(item.to_public(), sort_keys=True)))
            seq = cur.lastrowid
            db.execute("INSERT INTO agent_obs_dedupe(dedupe_key,content_hash,first_seq) VALUES(?,?,?)",
                       (item.dedupe_key, chash, seq))
            db.execute("""INSERT INTO agent_obs_event_ids(source,event_id,dedupe_key,content_hash,first_seq)
                          VALUES(?,?,?,?,?)""", (item.source, item.event_id, item.dedupe_key, chash, seq))
            self._bump(db, "accepted")
            if late:
                self._bump(db, "late_arrivals")
            # Bounded growth: drop oldest events; dedupe ledger is kept far
            # longer so a replay of a pruned event is still recognized.
            db.execute("DELETE FROM agent_obs_events WHERE seq <= ? - ?", (seq, self.max_events))
            db.execute("DELETE FROM agent_obs_dedupe WHERE first_seq <= ? - ?", (seq, self.max_dedupe))
            db.execute("DELETE FROM agent_obs_event_ids WHERE first_seq <= ? - ?", (seq, self.max_dedupe))
            return IngestResult("ACCEPTED", seq=seq, late=late)

    def ingest_many(self, raws: Iterable[Any], *, observed_at: datetime | None = None) -> dict[str, int]:
        out = {"ACCEPTED": 0, "DUPLICATE": 0, "QUARANTINED": 0}
        for raw in raws:
            out[self.ingest(raw, observed_at=observed_at).status] += 1
        return out

    def events(self, task_id: str, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute("""SELECT seq,late,event_json FROM agent_obs_events
                                 WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?""",
                              (task_id, int(after_seq), limit)).fetchall()
        out = []
        for r in rows:
            ev = json.loads(r["event_json"])
            ev["seq"] = r["seq"]
            ev["late_arrival"] = bool(r["late"])
            out.append(ev)
        return out

    def all_task_events(self, task_id: str) -> list[dict[str, Any]]:
        out, cursor = [], 0
        while True:
            page = self.events(task_id, after_seq=cursor, limit=500)
            out.extend(page)
            if len(page) < 500:
                return out
            cursor = page[-1]["seq"]

    def task_ids(self, limit: int = 200) -> list[str]:
        with self._connect() as db:
            return [r[0] for r in db.execute(
                """SELECT task_id FROM agent_obs_events GROUP BY task_id
                   ORDER BY MAX(seq) DESC LIMIT ?""", (max(1, min(limit, 1000)),))]

    def quarantine(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT at,reason,detail,event_id,source,flags_json
                                 FROM agent_obs_quarantine ORDER BY id DESC LIMIT ?""",
                              (max(1, min(limit, 500)),)).fetchall()
        return [{**dict(r), "flags": json.loads(r["flags_json"])} for r in rows]

    def counters(self) -> dict[str, int]:
        with self._connect() as db:
            out = {r["name"]: r["value"] for r in db.execute("SELECT * FROM agent_obs_counters")}
            out["stored_events"] = db.execute("SELECT COUNT(*) FROM agent_obs_events").fetchone()[0]
        return out


# --------------------------------------------------------------------------
# T008 — explicit dispatch / execution / delivery dimensions with freshness
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FreshnessPolicy:
    stale_after: timedelta = timedelta(minutes=15)   # no evidence -> UNKNOWN
    stall_after: timedelta = timedelta(minutes=10)   # alive but no progress -> STALLED

    def __post_init__(self) -> None:
        if self.stall_after <= timedelta(0) or self.stale_after <= timedelta(0):
            raise ObservabilityError("thresholds must be positive")


def _rank(ev: dict[str, Any]) -> int:
    return _RANK.get(ev.get("provenance_level"), -1)


def _key(ev: dict[str, Any]) -> tuple[datetime, int]:
    return (_aware(ev["occurred_at"]), int(ev.get("seq", 0)))


def _session_execution(evs: list[dict[str, Any]], at: datetime,
                       policy: FreshnessPolicy) -> dict[str, Any]:
    """Execution state of ONE session/agent identity. `evs` is sorted and
    belongs to a single session_id (identity binding guarantees one
    node/project/task/agent per session)."""
    reasons: list[str] = []
    verified = [e for e in evs if e["kind"] in EXEC_KINDS and _rank(e) >= _RANK["LOCAL_OBSERVED"]]
    negative_attested = [e for e in evs if e["kind"] in EXEC_TERMINAL_KINDS | {"waiting_input", "node_disconnected"}
                         and _rank(e) == _RANK["CENTRAL_ATTESTED"]]
    last_verified = max((_key(e)[0] for e in verified), default=None)
    last_progress = max((_key(e)[0] for e in verified if e["kind"] in PROGRESS_KINDS), default=None)
    last_heartbeat = max((_key(e)[0] for e in verified if e["kind"] in LIVENESS_KINDS), default=None)
    first_alive = min((_key(e)[0] for e in verified if e["kind"] in LIVENESS_KINDS | PROGRESS_KINDS),
                      default=None)
    # The latest "state-changing" verified event decides discrete states.
    # Heartbeats/progress never revive a session that already exited.
    state_evs = [e for e in verified if e["kind"] not in LIVENESS_KINDS | PROGRESS_KINDS]
    latest_state = state_evs[-1] if state_evs else None
    if latest_state and last_verified and _key(latest_state)[0] < last_verified and \
            latest_state["kind"] in {"waiting_input", "node_disconnected", "status_unknown"}:
        latest_state = None  # newer liveness/progress supersedes a soft state
    execution = "UNKNOWN"
    if not verified:
        reasons.append("no_locally_verified_execution_evidence")
        if negative_attested:
            reasons.append(f"attested_{negative_attested[-1]['kind']}")
    elif latest_state and latest_state["kind"] in {"node_disconnected", "status_unknown"}:
        reasons.append(latest_state["kind"])
    elif latest_state and latest_state["kind"] == "process_failed":
        execution = "FAILED"
    elif latest_state and latest_state["kind"] == "process_exited":
        execution = "FAILED" if latest_state.get("result") in {"FAIL", "ERROR"} else "EXITED"
    elif at - last_verified > policy.stale_after:
        reasons.append("stale_source")
    elif latest_state and latest_state["kind"] == "waiting_input":
        execution = "WAITING_FOR_INPUT"
    elif last_progress and at - last_progress <= policy.stall_after:
        execution = "RUNNING_VERIFIED"
    elif last_heartbeat and at - last_heartbeat <= policy.stall_after:
        since = last_progress or first_alive
        if at - since > policy.stall_after:
            execution = "STALLED"
            reasons.append("liveness_without_progress" if last_progress
                           else "heartbeat_only_no_progress_ever")
        else:
            reasons.append("alive_awaiting_first_progress")
    else:
        reasons.append("no_recent_liveness_or_progress")
    terminal = execution in ("EXITED", "FAILED")
    # "current" = non-terminal and has fresh locally verified evidence.
    current = (not terminal and last_verified is not None
               and at - last_verified <= policy.stale_after
               and "stale_source" not in reasons)
    return {"execution": execution, "reasons": reasons, "terminal": terminal, "current": current,
            "last_verified": last_verified, "last_progress": last_progress,
            "last_event": max(_key(e)[0] for e in evs)}


# Most attention-needing first. Among CURRENT (live) sessions the task is
# only as healthy as its least healthy session.
_LIVE_SEVERITY = ("UNKNOWN", "WAITING_FOR_INPUT", "STALLED", "RUNNING_VERIFIED")


def _aggregate_execution(per: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    if not per:
        return "UNKNOWN", []
    live = [p for p in per.values() if p["current"]]
    if live:
        chosen = min(live, key=lambda p: _LIVE_SEVERITY.index(p["execution"]))
    else:
        # Nothing live: the most recently active session decides (a newer
        # clean retry supersedes an older failure, a newer failure wins
        # over an older clean exit). Failed siblings stay as blockers.
        chosen = max(per.values(), key=lambda p: p["last_verified"] or p["last_event"])
    return chosen["execution"], list(chosen["reasons"])


def _stage_for(dispatch: str, milestones: dict[str, bool]) -> str:
    stage = "NOT_STARTED"
    if dispatch == "ACKNOWLEDGED":
        stage = "CODING"
    for st, m in (("TESTING", "tests_passed"), ("REVIEW", "review_passed"),
                  ("QA", "qa_passed"), ("MERGED", "merged"),
                  ("DEPLOYED", "deployed"), ("ACCEPTED", "accepted")):
        if milestones.get(m):
            stage = st
    return stage


MAX_HISTORIC_EVIDENCE = 50


def derive_task_view(events: list[dict[str, Any]], *, at: datetime,
                     policy: FreshnessPolicy = FreshnessPolicy()) -> dict[str, Any]:
    """Pure function. Activity volume never touches completion; completion is
    attached separately from Harness (attach_harness). Execution is derived
    independently PER session, then aggregated conservatively, so one
    session's events can never override another session's state."""
    at = _aware(at)
    evs = sorted(events, key=_key)
    reasons: list[str] = []
    blockers: list[str] = []

    # Dispatch dimension
    dispatch = "QUEUED"
    if any(e["kind"] in DISPATCH_KINDS - {"task_queued"} for e in evs):
        dispatch = "DISPATCHED_UNVERIFIED"
    if any((e["kind"] in LIVENESS_KINDS | PROGRESS_KINDS) and _rank(e) >= _RANK["LOCAL_OBSERVED"]
           for e in evs):
        dispatch = "ACKNOWLEDGED"

    # Execution dimension: only LOCAL_OBSERVED+ may certify a live state.
    # CENTRAL_ATTESTED / self-reports may only push toward UNKNOWN/negative.
    by_session: dict[str, list[dict[str, Any]]] = {}
    for e in evs:
        if e["kind"] in EXEC_KINDS:  # evidence-only lanes carry no execution state
            by_session.setdefault(e["session_id"], []).append(e)
    per = {sid: _session_execution(sevs, at, policy) for sid, sevs in by_session.items()}
    if per:
        execution, exec_reasons = _aggregate_execution(per)
        reasons.extend(exec_reasons)
    else:
        execution = "UNKNOWN"
        reasons.append("no_locally_verified_execution_evidence")
    for sid, p in sorted(per.items()):
        if p["execution"] == "FAILED":
            blockers.append(f"SESSION_FAILED:{sid}")
    verified_times = [p["last_verified"] for p in per.values() if p["last_verified"]]
    progress_times = [p["last_progress"] for p in per.values() if p["last_progress"]]
    last_any = max((_key(e)[0] for e in evs), default=None)
    last_verified = max(verified_times, default=None)
    last_progress = max(progress_times, default=None)

    # Delivery milestones: each independent; FAIL never counts; a later FAIL
    # of the same milestone supersedes an older PASS. Evidence whose
    # currency (exact commit/brief) is HISTORIC/UNKNOWN is history only.
    milestones = {"submitted": dispatch != "QUEUED",
                  "running_verified": execution == "RUNNING_VERIFIED"}
    historic: list[dict[str, Any]] = []
    for kind, (name, minimum) in MILESTONE_KINDS.items():
        rel = [e for e in evs if e["kind"] == kind and _rank(e) >= _RANK[minimum]
               and e.get("result") in {"PASS", "FAIL", "ERROR"}]
        cur = [e for e in rel if e.get("evidence_currency") in (None, "CURRENT")]
        milestones[name] = bool(cur) and cur[-1]["result"] == "PASS"
        historic += [{"ref": e.get("sanitized_evidence_ref"), "kind": kind, "result": e.get("result"),
                      "currency": e.get("evidence_currency"), "session_id": e["session_id"],
                      "worktree_ref": e.get("worktree_ref"), "occurred_at": e["occurred_at"]}
                     for e in rel if e.get("evidence_currency") not in (None, "CURRENT")]
        ignored = [e for e in evs if e["kind"] == kind and _rank(e) < _RANK[minimum]]
        if ignored:
            reasons.append(f"{name}_claim_below_required_provenance")
    if historic:
        reasons.append("evidence_currency_unverified")
    historic.sort(key=lambda h: h["occurred_at"])
    age = (at - last_any).total_seconds() if last_any else None
    return {
        "dispatch": dispatch,
        "execution": execution,
        "delivery_stage": _stage_for(dispatch, milestones),
        "milestones": milestones,
        "reasons": reasons,
        "blockers": blockers,
        "sessions": [{"session_id": sid, "execution": p["execution"], "reasons": p["reasons"]}
                     for sid, p in sorted(per.items())],
        "historic_evidence": historic[-MAX_HISTORIC_EVIDENCE:],
        "freshness": {
            "last_event_at": _iso(last_any) if last_any else None,
            "last_verified_at": _iso(last_verified) if last_verified else None,
            "last_progress_at": _iso(last_progress) if last_progress else None,
            "age_seconds": int(age) if age is not None else None,
            "stale": last_verified is None or (at - last_verified) > policy.stale_after,
        },
        "event_count": len(evs),
        "late_arrivals": sum(bool(e.get("late_arrival")) for e in evs),
        "completion_percent": None,
        "completion_source": "UNKNOWN: no reviewed Harness gate plan linked",
    }


# --------------------------------------------------------------------------
# Current gate truth: reuse TaskDecisionService (never a second gate model)
# --------------------------------------------------------------------------

_GATE_CHECKLIST = (("tests", "AUTOMATED_TESTS", "tests_passed"), ("review", "REVIEW", "review_passed"),
                   ("qa", "RUNTIME_VERIFICATION", "qa_passed"), ("merge", "MERGE", "merged"))
_CHECKLIST_GATE = {"done": "PASS", "skipped": "NOT_REQUIRED", "blocked": "BLOCKED"}


def gate_truth_from_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Project TaskDecisionService.evaluate()'s evidence-based checklist
    (exact-HEAD tests, commit/brief-pinned review, current QA, required
    merges) into observability gates. No gate logic is recomputed here;
    worktree paths are never copied out."""
    states = {i.get("key"): i.get("state") for i in decision.get("checklist") or [] if isinstance(i, dict)}
    gates = {name: _CHECKLIST_GATE.get(states.get(key), "NOT_CURRENT") for name, key, _m in _GATE_CHECKLIST}
    return {"source": "TaskDecisionService.evaluate", "status": "KNOWN",
            "task_status": decision.get("status"), **gates,
            "workspaces": [{"workspace_ref": f"pf-workspace-{b.get('id')}",
                            "review_status": b.get("review_status"),
                            "head": (b.get("head") or "")[:12] or None}
                           for b in decision.get("builders") or [] if isinstance(b, dict)]}


def apply_gate_truth(view: dict[str, Any], truth: dict[str, Any] | None) -> dict[str, Any]:
    """Milestones tests/review/qa/merged become PASS only when the current
    TaskDecisionService gate says PASS. Without gate truth they stay False
    (UNKNOWN), whatever historic PASS rows exist. Never sets completion."""
    out = dict(view)
    out["milestones"] = dict(view.get("milestones", {}))
    out["reasons"] = list(view.get("reasons", []))
    if truth is None:
        out["gates"] = {"source": None, "status": "UNKNOWN",
                        **{name: "UNKNOWN" for name, _k, _m in _GATE_CHECKLIST}, "workspaces": []}
        out["reasons"].append("gate_truth_unavailable")
    else:
        out["gates"] = truth
    for name, _key, milestone in _GATE_CHECKLIST:
        out["milestones"][milestone] = out["gates"].get(name) == "PASS"
    out["delivery_stage"] = _stage_for(out.get("dispatch", "QUEUED"), out["milestones"])
    return out


# --------------------------------------------------------------------------
# T009 — reuse Harness gate completion + immutable child TTL (no new math)
# --------------------------------------------------------------------------

HARNESS_SNAPSHOT_SCHEMA = "harness-progress-v1"
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
DEFAULT_HARNESS_SNAPSHOT = Path.home() / ".local/state/projectflow-harness/monitor-progress.json"
DEFAULT_HARNESS_REGISTRY = Path(__file__).resolve().parents[2] / "config/harness_monitor_registry.json"
AUTOMATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,90}$")  # same rule as harness_monitor_bridge.ID_RE


def _read_small_json(path: Path, limit: int) -> Any:
    if path.is_symlink():
        raise ObservabilityError("symlink refused")
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if path.stat().st_size > limit:
        raise ObservabilityError("file too large")
    return json.loads(path.read_text(encoding="utf-8"))


def load_harness_snapshot(path: str | Path | None) -> tuple[dict[str, Any] | None, str]:
    """Read the private snapshot that the EXISTING Harness timer already
    writes (scripts/harness_progress.py --write). Read-only: does not call
    summarize() (which opens HarnessStore and runs CREATE TABLE/WAL)."""
    if path is None:
        return None, "Harness progress snapshot path not configured"
    try:
        data = _read_small_json(Path(path), MAX_SNAPSHOT_BYTES)
    except FileNotFoundError:
        return None, "Harness progress snapshot absent on this node"
    except (OSError, ValueError, ObservabilityError):
        return None, "Harness progress snapshot unreadable or malformed"
    if not isinstance(data, dict) or data.get("schema") != HARNESS_SNAPSHOT_SCHEMA \
            or not isinstance(data.get("monitors"), list):
        return None, "Harness progress snapshot has unsupported schema"
    return data, "ok"


def _enabled_automations(registry_path: Path) -> set[str]:
    data = _read_small_json(registry_path, MAX_SNAPSHOT_BYTES)
    if not isinstance(data, dict) or data.get("schema_version") != 1 or \
            not isinstance(data.get("automations"), list):
        raise ObservabilityError("unsupported Harness registry")
    return {a["automation_id"] for a in data["automations"]
            if isinstance(a, dict) and a.get("configured_enabled") is True
            and isinstance(a.get("automation_id"), str)}


def load_task_links(path: str | Path | None, *,
                    registry_path: str | Path = DEFAULT_HARNESS_REGISTRY) -> tuple[dict[int, dict[str, Any]], str]:
    """Explicit, operator-reviewed ProjectFlow task -> Harness automation /
    child links. No such mapping exists elsewhere in the architecture, and
    it is NEVER inferred (titles, repos, goals). Absent file -> no links.
    Any invalid entry fails the WHOLE file closed (no partial trust)."""
    if path is None:
        return {}, "no reviewed ProjectFlow task -> Harness link file is configured"
    try:
        data = _read_small_json(Path(path), 1024 * 1024)
    except FileNotFoundError:
        return {}, "reviewed ProjectFlow task -> Harness link file absent"
    except (OSError, ValueError, ObservabilityError):
        return {}, "link file unreadable or malformed"
    try:
        enabled = _enabled_automations(Path(registry_path))
    except (OSError, ValueError, ObservabilityError):
        return {}, "Harness registry unreadable; links refused"
    if not isinstance(data, dict) or data.get("schema_version") != 1 or \
            not isinstance(data.get("links"), list):
        return {}, "link file has unsupported schema"
    out: dict[int, dict[str, Any]] = {}
    for entry in data["links"]:
        if not isinstance(entry, dict):
            return {}, "link file invalid: entry is not an object"
        tid, aid, hid = entry.get("pf_task_id"), entry.get("automation_id"), entry.get("harness_task_id")
        if type(tid) is not int or tid <= 0 or tid in out:
            return {}, "link file invalid: pf_task_id missing/duplicate"
        if not isinstance(aid, str) or not AUTOMATION_ID_RE.match(aid) or aid not in enabled:
            return {}, "link file invalid: automation_id not an enabled Harness monitor"
        if hid is not None and (type(hid) is not int or hid <= 0):
            return {}, "link file invalid: harness_task_id"
        ref = entry.get("proof_ref")
        if not isinstance(ref, str) or not REF_RE.match(ref) or looks_sensitive(ref):
            return {}, "link file invalid: proof_ref required (table:id / pr:number style)"
        try:
            datetime.strptime(str(entry.get("reviewed_on")), "%Y-%m-%d")
        except ValueError:
            return {}, "link file invalid: reviewed_on date required"
        out[tid] = {"automation_id": aid, "harness_task_id": hid, "proof_ref": ref,
                    "reviewed_on": entry["reviewed_on"]}
    return out, "ok"


def _unknown_budget(reason: str) -> dict[str, Any]:
    return {"status": "UNKNOWN", "reason": reason, "time_used_percent": None}


def attach_harness(view: dict[str, Any], *, snapshot: dict[str, Any] | None,
                   automation_id: str | None, harness_task_id: int | None = None,
                   at: datetime | None = None, health_max_age: int = 900,
                   unknown_reason: str | None = None) -> dict[str, Any]:
    """Copy (never recompute) completion % and child TTL from the existing
    scripts/harness_progress.summarize() snapshot. Stale/absent snapshot,
    missing link or unhealthy service -> UNKNOWN (never 0%, never the last
    green value). `time_budget` is always present and independent of %."""
    out = dict(view)
    out["blockers"] = list(view.get("blockers", []))
    out["reasons"] = list(view.get("reasons", []))
    out["harness_link"] = {"automation_id": automation_id, "harness_task_id": harness_task_id}
    if snapshot is None or automation_id is None:
        why = unknown_reason or ("no validated Harness link for this task" if automation_id is None
                                 else "Harness snapshot unavailable")
        out["completion_percent"] = None
        out["completion_source"] = f"UNKNOWN: {why}"
        out["time_budget"] = _unknown_budget(why)
        return out
    health = snapshot.get("health") or {}
    try:
        snap_age = (_aware(at or datetime.now(timezone.utc)) - _aware(snapshot["observed_at"])).total_seconds()
    except (KeyError, ObservabilityError, ValueError, TypeError):
        snap_age = None
    monitor = next((m for m in snapshot.get("monitors", [])
                    if isinstance(m, dict) and m.get("id") == automation_id), None)
    fresh = not (snap_age is None or snap_age > health_max_age or snap_age < -300
                 or health.get("status") != "HEALTHY")
    if not fresh:
        out["completion_percent"] = None
        out["completion_source"] = "UNKNOWN: Harness snapshot stale or service not HEALTHY"
        out["blockers"].append("HARNESS_EVIDENCE_STALE")
        out["time_budget"] = _unknown_budget("Harness snapshot stale or service not HEALTHY")
        return out
    if monitor is None:
        out["completion_percent"] = None
        out["completion_source"] = "UNKNOWN: linked automation not in Harness snapshot"
        out["time_budget"] = _unknown_budget("linked automation not in Harness snapshot")
        return out
    pct = monitor.get("completion_percent")
    if monitor.get("kind") == "maintenance" or pct is None:
        out["completion_percent"] = None
        out["completion_source"] = "N/A: maintenance monitor has no completion %"
    elif type(pct) is int and 0 <= pct <= 100:
        out["completion_percent"] = pct
        out["completion_source"] = ("harness_progress: PASSED evidence gates only "
                                    f"({monitor.get('gates_passed')}/{monitor.get('gates_total')})")
    else:
        out["completion_percent"] = None
        out["completion_source"] = "UNKNOWN: malformed Harness completion value"
    if harness_task_id is None:
        out["time_budget"] = _unknown_budget("no Harness child task linked")
        return out
    clock = next((t for t in list(monitor.get("active_tasks", [])) + list(monitor.get("recent_tasks", []))
                  if isinstance(t, dict) and t.get("id") == harness_task_id), None)
    if clock is None:
        out["time_budget"] = _unknown_budget("linked Harness child not in this monitor's snapshot")
        return out
    out["time_budget"] = {"status": "KNOWN", **{k: clock.get(k) for k in (
        "state", "cleanup", "time_used_percent", "time_used_label",
        "time_budget_label", "remaining_label", "deadline_local", "overdue", "finished")}}
    if clock.get("cleanup") == "CLEANUP_PENDING" or clock.get("state") == "EXPIRED" or clock.get("overdue"):
        out["blockers"].append("CLEANUP_PENDING" if clock.get("cleanup") == "CLEANUP_PENDING"
                               else "TTL_EXPIRED")
        if out.get("execution") == "RUNNING_VERIFIED":
            out["execution"] = "UNKNOWN"
            out["reasons"].append("ttl_expired_overrides_running")
    return out


def harness_view_for_task(view: dict[str, Any], *, task_id: int, at: datetime,
                          snapshot_path: str | Path | None, links_path: str | Path | None,
                          registry_path: str | Path = DEFAULT_HARNESS_REGISTRY) -> dict[str, Any]:
    """Resolve the explicit link, then attach the existing Harness snapshot.
    Pure reads; no Harness DB connection at all."""
    links, why = load_task_links(links_path, registry_path=registry_path)
    link = links.get(task_id)
    if link is None:
        return attach_harness(view, snapshot=None, automation_id=None, at=at,
                              unknown_reason=why if why != "ok" else "task has no reviewed Harness link")
    snapshot, swhy = load_harness_snapshot(snapshot_path)
    out = attach_harness(view, snapshot=snapshot, automation_id=link["automation_id"],
                         harness_task_id=link["harness_task_id"], at=at,
                         unknown_reason=None if snapshot else swhy)
    out["harness_link"]["proof_ref"] = link["proof_ref"]
    return out


# --------------------------------------------------------------------------
# Read-only adapters (T006): existing AgentSession lifecycle + Harness ledger
# --------------------------------------------------------------------------

def _ro_connect(db_path: str | Path) -> sqlite3.Connection:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def _ts(value: str | None) -> str | None:
    if not value:
        return None
    text = value.replace(" ", "T")
    obj = datetime.fromisoformat(text)
    if obj.tzinfo is None:  # SQLite CURRENT_TIMESTAMP is UTC
        obj = obj.replace(tzinfo=timezone.utc)
    return _iso(obj)


def _stamp(iso: str) -> str:
    """ID-safe compact UTC stamp for stable event ids."""
    return _aware(iso).strftime("%Y%m%dT%H%M%S%fZ")


# (table, kind, columns needed, status column, time column, PASS values, FAIL values)
_EVIDENCE_TABLES = (
    ("review_runs", "review_result", "status", "completed_at", {"PASS"}, {"FIX_REQUIRED", "FAIL"}),
    ("qa_runs", "qa_result", "status", "completed_at", {"PASS"}, {"FAIL"}),
    ("merge_records", "merge_recorded", "merge_status", "merged_at", {"MERGED"}, {"FAILED"}),
    ("deployments", "deploy_recorded", "status", "finished_at", {"VERIFIED"}, {"FAILED", "ROLLED_BACK"}),
)
MAX_SESSIONS = 50
MAX_EVIDENCE_ROWS = 200
MAX_WORKSPACES = 50


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}  # table names are constants


def projectflow_task_events(db_path: str | Path, *, task_id: int, node_id: str, project_id: str,
                            session_limit: int = MAX_SESSIONS,
                            evidence_limit: int = MAX_EVIDENCE_ROWS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read-only (sqlite mode=ro), TASK-BOUNDED projection of agent_sessions
    and evidence tables into raw events.

    Every SQL statement is filtered by this one task_id (or by the agent
    workspace ids that belong to it) and has a LIMIT, using existing
    indices (idx_agent_sessions_task, idx_review_runs_task, idx_qa_runs_task,
    idx_merge_records_task, idx_deployments_lookup, idx_agent_task,
    idx_test_entity). Never selects cwd, transcript_tail, prompts,
    stdout/stderr, findings, notes, URLs or commands. Missing tables/columns
    (older schema) are skipped and reported in meta, not guessed.
    Returns (raw_events, meta)."""
    if type(task_id) is not int or task_id <= 0:
        raise ObservabilityError("task_id must be a positive int")
    session_limit = max(1, min(int(session_limit), MAX_SESSIONS))
    evidence_limit = max(1, min(int(evidence_limit), MAX_EVIDENCE_ROWS))
    out: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"task_id": task_id, "truncated": [], "skipped_sources": [],
                            "session_count": 0, "workspace_count": 0}
    base = {"schema_version": SCHEMA_VERSION, "node_id": node_id, "project_id": project_id,
            "task_id": f"pf-task-{task_id}"}
    db = _ro_connect(db_path)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sessions = []
        if "agent_sessions" in tables:
            sessions = db.execute(
                """SELECT id,task_id,workspace_id,agent,status,started_at,last_activity_at,
                          exited_at,exit_code FROM agent_sessions
                   WHERE task_id=? ORDER BY id DESC LIMIT ?""",
                (task_id, session_limit + 1)).fetchall()
            if len(sessions) > session_limit:
                meta["truncated"].append("agent_sessions")
                sessions = sessions[:session_limit]
        else:
            meta["skipped_sources"].append("agent_sessions")
        meta["session_count"] = len(sessions)
        for s in sessions:
            ident = {**base, "session_id": f"pf-session-{s['id']}",
                     "agent_id": f"{s['agent']}-{s['id']}",
                     "worktree_ref": f"pf-workspace-{s['workspace_id']}",
                     "source": "projectflow_agent_session",
                     "provenance_level": "LOCAL_OBSERVED"}
            started = _ts(s["started_at"])
            if started:
                out.append({**ident, "event_id": f"pf-s{s['id']}-started",
                            "kind": "session_started", "occurred_at": started})
            if s["last_activity_at"] and s["status"] in ("RUNNING", "WAITING_FOR_INPUT"):
                at = _ts(s["last_activity_at"])
                out.append({**ident, "event_id": f"pf-s{s['id']}-out-{_stamp(at)}",
                            "kind": "output_growth", "occurred_at": at})
            if s["status"] == "WAITING_FOR_INPUT" and s["last_activity_at"]:
                at = _ts(s["last_activity_at"])
                out.append({**ident, "event_id": f"pf-s{s['id']}-wait-{_stamp(at)}",
                            "kind": "waiting_input", "occurred_at": at})
            if s["status"] in ("EXITED", "FAILED") and s["exited_at"]:
                failed = s["status"] == "FAILED" or (s["exit_code"] not in (None, 0))
                out.append({**ident, "event_id": f"pf-s{s['id']}-end",
                            "kind": "process_failed" if s["status"] == "FAILED" else "process_exited",
                            "result": "FAIL" if failed else "PASS",
                            "occurred_at": _ts(s["exited_at"])})
        # Workspaces that belong to THIS task (bounded). test_runs has no
        # task_id, so it is filtered by these ids; review/qa rows keep their
        # own workspace as owner only when it is one of these.
        ws_ids: set[int] = {s["workspace_id"] for s in sessions if s["workspace_id"] is not None}
        if "agent_workspaces" in tables and "task_id" in _columns(db, "agent_workspaces"):
            ws_ids |= {r[0] for r in db.execute(
                "SELECT id FROM agent_workspaces WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, MAX_WORKSPACES))}
        ws_ids = set(sorted(ws_ids, reverse=True)[:MAX_WORKSPACES])
        meta["workspace_count"] = len(ws_ids)

        def lane(wid: Any) -> dict[str, str]:
            # Evidence is NEVER relabeled onto an agent session (the latest
            # session did not necessarily produce it). It lives on its own
            # workspace-owned lane, or a task-scoped lane, never another task.
            if type(wid) is int and wid in ws_ids:
                return {"session_id": f"pf-workspace-{wid}-evidence",
                        "agent_id": f"projectflow-evidence-ws{wid}",
                        "worktree_ref": f"pf-workspace-{wid}"}
            return {"session_id": f"pf-task-{task_id}-evidence", "agent_id": "projectflow-evidence"}

        def evidence(kind: str, table: str, rid: int, status: str, at: str | None,
                     pass_values: set[str], fail_values: set[str], wid: Any = None) -> None:
            if not at:
                return
            result = "PASS" if status in pass_values else "FAIL" if status in fail_values else "PENDING"
            # A raw row cannot prove it matches the CURRENT commit/brief;
            # currency is decided only by TaskDecisionService (apply_gate_truth).
            out.append({**base, **lane(wid), "source": "projectflow_evidence",
                        "provenance_level": "LOCAL_VERIFIED", "evidence_currency": "UNKNOWN",
                        "event_id": f"pf-{table}-{rid}-{status}", "kind": kind, "result": result,
                        "sanitized_evidence_ref": f"{table}:{rid}", "occurred_at": _ts(at)})

        for table, kind, status_col, time_col, ok, bad in _EVIDENCE_TABLES:
            cols = _columns(db, table) if table in tables else set()
            if not {"id", "task_id", status_col, time_col} <= cols:
                meta["skipped_sources"].append(table)
                continue
            wcol = "workspace_id" if "workspace_id" in cols else "NULL"
            rows = db.execute(
                f"SELECT id,{status_col} AS st,{time_col} AS at,{wcol} AS wid FROM {table} "
                "WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, evidence_limit + 1)).fetchall()
            if len(rows) > evidence_limit:
                meta["truncated"].append(table)
                rows = rows[:evidence_limit]
            for r in reversed(rows):
                evidence(kind, table, r["id"], r["st"], r["at"], ok, bad, r["wid"])

        if "test_runs" not in tables or not {"id", "workspace_type", "workspace_id", "status",
                                               "finished_at"} <= _columns(db, "test_runs"):
            meta["skipped_sources"].append("test_runs")
        elif ws_ids:
            marks = ",".join("?" * len(ws_ids))
            rows = db.execute(
                f"""SELECT id,status,finished_at,workspace_id FROM test_runs
                    WHERE workspace_type='agent' AND workspace_id IN ({marks})
                    ORDER BY id DESC LIMIT ?""", (*sorted(ws_ids), evidence_limit + 1)).fetchall()
            if len(rows) > evidence_limit:
                meta["truncated"].append("test_runs")
                rows = rows[:evidence_limit]
            for r in reversed(rows):
                evidence("test_result", "test_runs", r["id"], r["status"], r["finished_at"],
                         {"PASSED", "PASS"}, {"FAILED", "FAIL", "ERROR"}, r["workspace_id"])
    finally:
        db.close()
    return out, meta


HARNESS_STATE_KIND = {
    "NOT_RUNNING": "process_exited",
    "BLOCKED": "waiting_input",
    "FAILED": "process_failed",
}


def harness_task_events(db_path: str | Path, *, project_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Read-only projection of the Harness ledger. DISPATCHED is only
    dispatch evidence; RUNNING_VERIFIED observations are already gated by
    HarnessStore.observe (live process + progress) so they map to a progress
    event; STALLED maps to a heartbeat (alive, no progress). DONE_VERIFIED
    is technical only and never becomes business acceptance here."""
    db = _ro_connect(db_path)
    out: list[dict[str, Any]] = []
    try:
        rows = db.execute("""SELECT e.id,e.task_id,e.at,e.event,e.evidence_json,t.agent,t.node
                             FROM harness_events e JOIN harness_tasks t ON t.id=e.task_id
                             ORDER BY e.id DESC LIMIT ?""", (limit,)).fetchall()
    finally:
        db.close()
    for r in reversed(rows):
        try:
            evidence = json.loads(r["evidence_json"])
        except ValueError:
            evidence = {}
        state = evidence.get("state") if isinstance(evidence, dict) else None
        base = {"schema_version": SCHEMA_VERSION, "node_id": r["node"], "project_id": project_id,
                "task_id": f"harness-{r['task_id']}", "session_id": f"harness-{r['task_id']}",
                "agent_id": f"{r['agent']}-h{r['task_id']}", "source": "harness_store",
                "occurred_at": r["at"], "event_id": f"harness-ev-{r['id']}",
                "sanitized_evidence_ref": f"harness_events:{r['id']}"}
        if r["event"] == "DISPATCHED":
            out.append({**base, "kind": "prompt_submitted", "provenance_level": "CENTRAL_ATTESTED"})
        elif r["event"] == "OBSERVED" and state == "RUNNING_VERIFIED":
            out.append({**base, "kind": "output_growth", "provenance_level": "LOCAL_OBSERVED"})
        elif r["event"] == "OBSERVED" and state == "STALLED":
            out.append({**base, "kind": "heartbeat", "provenance_level": "LOCAL_OBSERVED"})
        elif r["event"] == "OBSERVED" and state in HARNESS_STATE_KIND:
            out.append({**base, "kind": HARNESS_STATE_KIND[state], "provenance_level": "LOCAL_OBSERVED"})
        elif r["event"] == "OBSERVED":
            out.append({**base, "kind": "status_unknown", "provenance_level": "LOCAL_OBSERVED"})
        elif r["event"] in {"FAILED", "EXPIRED"}:
            out.append({**base, "kind": "process_failed", "result": "FAIL",
                        "provenance_level": "LOCAL_OBSERVED"})
        # DONE_VERIFIED / cleanup events are technical-only: they are shown
        # through attach_harness() TTL/cleanup data, never as acceptance.
    return out


def _timeline_key(ev: dict[str, Any]) -> tuple[datetime, str]:
    """Total, input-order-independent timeline order. dedupe_key is unique
    after dedupe, so equal timestamps are still strictly ordered."""
    return (_aware(ev["occurred_at"]), str(ev["dedupe_key"]))


def view_from_raw(raws: Iterable[Any], *, at: datetime,
                  policy: FreshnessPolicy = FreshnessPolicy(),
                  expected: dict[str, str] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """In-memory normalize + dedupe + derive, with NO persistence. Used by
    GET routes so a read never writes (spec plan: no mutation from GET).
    Applies the same conflict rules as ObservabilityEventStore.ingest
    (dedupe_key/event_id conflicts, session identity binding) and, when
    `expected` is given, drops events for any other node/project/task.
    Returns (view, events in stable timeline order, quarantined_count)."""
    seen: dict[str, str] = {}
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    bindings: dict[str, tuple[str, ...]] = {}
    events: list[dict[str, Any]] = []
    quarantined = 0
    for raw in raws:
        item = normalize(raw, observed_at=at)
        if isinstance(item, Quarantine):
            quarantined += 1
            continue
        if expected and any(getattr(item, k) != v for k, v in expected.items()):
            quarantined += 1  # tenant/task binding: never shown on another task
            continue
        h = item.content_hash()
        if item.dedupe_key in seen:
            quarantined += seen[item.dedupe_key] != h
            continue
        owner = owners.get((item.source, item.event_id))
        if owner is not None:
            quarantined += 1  # event_id reused with another dedupe_key
            continue
        ident = tuple(getattr(item, k) for k in ("node_id", "project_id", "task_id", "agent_id"))
        if bindings.setdefault(item.session_id, ident) != ident:
            quarantined += 1  # identity_conflict: no relabeling
            continue
        seen[item.dedupe_key] = h
        owners[(item.source, item.event_id)] = (item.dedupe_key, h)
        events.append({**item.to_public(), "late_arrival": False})
    events.sort(key=_timeline_key)
    for i, e in enumerate(events, start=1):
        e["seq"] = i  # display ordinal only; paging uses the opaque cursor
    view = derive_task_view(events, at=at, policy=policy)
    view["quarantined"] = quarantined
    return view, events, quarantined


CURSOR_PREFIX = "v1."
MAX_CURSOR_LEN = 512
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def encode_cursor(ev: dict[str, Any]) -> str:
    occurred, key = _timeline_key(ev)
    body = json.dumps([_iso(occurred), key], separators=(",", ":")).encode()
    return CURSOR_PREFIX + base64.urlsafe_b64encode(body).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Opaque (occurred_at, dedupe_key) cursor. Anything malformed raises
    ObservabilityError (-> HTTP 400), never a silent restart or skip."""
    if not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_LEN or not cursor.startswith(CURSOR_PREFIX):
        raise ObservabilityError("malformed cursor")
    body = cursor[len(CURSOR_PREFIX):]
    if not _B64URL_RE.match(body):
        raise ObservabilityError("malformed cursor")
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ObservabilityError("malformed cursor") from None
    if not (isinstance(data, list) and len(data) == 2 and all(isinstance(x, str) for x in data)):
        raise ObservabilityError("malformed cursor")
    try:
        occurred = _aware(data[0])
    except (ObservabilityError, ValueError):
        raise ObservabilityError("malformed cursor") from None
    if not ID_RE.match(data[1]):
        raise ObservabilityError("malformed cursor")
    return occurred, data[1]


def page_events(events: list[dict[str, Any]], *, cursor: str | None, limit: int) -> dict[str, Any]:
    """Keyset page strictly after `cursor` in (occurred_at, dedupe_key)
    order. Same input + same cursor -> identical page. An event that
    arrives later with a timestamp before the cursor cannot appear on a
    forward page; instead `before_cursor_count` grows so a client can
    detect it and resync from the start (never silently lost)."""
    limit = max(1, min(int(limit), 200))
    ordered = sorted(events, key=_timeline_key)
    if cursor is None:
        after, before = ordered, 0
    else:
        ck = decode_cursor(cursor)
        after = [e for e in ordered if _timeline_key(e) > ck]
        before = len(ordered) - len(after)
    page = after[:limit]
    return {"events": page, "next_cursor": encode_cursor(page[-1]) if len(page) == limit else None,
            "before_cursor_count": before, "total": len(ordered)}


# --------------------------------------------------------------------------
# T011 (partial) — mobile operator page. Pure function of the view; every
# value is HTML-escaped. Two INDEPENDENT meters: verified completion (from
# Harness only) and child TTL time budget. UNKNOWN is text, never a bar.
# --------------------------------------------------------------------------

_PAGE_CSS = """
:root{--bg:#fff;--fg:#14171a;--muted:#5b6470;--line:#d6dae0;--card:#f7f8fa;
--ok:#1e7b46;--warn:#9a5b00;--bad:#b3261e;--track:#e6e9ee;--done:#2e8b57;--ttl:#c77700}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121417;--fg:#e8eaed;
--muted:#a0a8b3;--line:#333a42;--card:#1b1f24;--ok:#5cc98a;--warn:#f0b35a;--bad:#ff8a80;
--track:#2a3037;--done:#4fbf7f;--ttl:#f0a030}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:640px;margin:0 auto;padding:12px 16px 32px}
h1{font-size:18px;margin:4px 0 2px}h2{font-size:15px;margin:0 0 6px}
.sub{color:var(--muted);font-size:13px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;
overflow-wrap:anywhere;word-break:break-word;min-width:0}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:6px 10px}
.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}.v{font-weight:600}
.meter{height:12px;border-radius:6px;background:var(--track);overflow:hidden;margin:6px 0 4px}
.meter>span{display:block;height:100%}
.m-done>span{background:var(--done)}.m-ttl>span{background:var(--ttl)}
.unknown{border:1px dashed var(--muted);border-radius:6px;padding:6px 8px;color:var(--muted);font-weight:600}
.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 8px;font-size:12px;margin:2px 4px 2px 0}
.bad{color:var(--bad)}.ok{color:var(--ok)}.warn{color:var(--warn)}
ul{padding-left:18px;margin:4px 0}li{margin:3px 0}
.tl li{list-style:none;margin-left:-18px;border-left:2px solid var(--line);padding-left:8px}
.small{font-size:12px;color:var(--muted)}
"""


def render_operator_page(task_id: int, view: dict[str, Any], events: list[dict[str, Any]]) -> str:
    from html import escape as e
    fresh = view.get("freshness", {})
    comp = view.get("completion_percent")
    budget = view.get("time_budget") or _unknown_budget("not computed")
    exec_state = view.get("execution", "UNKNOWN")
    exec_cls = {"RUNNING_VERIFIED": "ok", "WAITING_FOR_INPUT": "warn", "STALLED": "warn",
                "FAILED": "bad", "UNKNOWN": "bad"}.get(exec_state, "")

    if type(comp) is int:
        comp_html = (f'<div class="v">{comp}% gate đã nghiệm thu (verified)</div>'
                     f'<div class="meter m-done" role="img" aria-label="verified completion {comp}%">'
                     f'<span style="width:{comp}%"></span></div>')
    else:
        comp_html = '<div class="unknown">UNKNOWN — không hiển thị % khi thiếu chứng cứ</div>'
    comp_html += f'<div class="small">{e(str(view.get("completion_source", "")))}</div>'

    used = budget.get("time_used_percent")
    if budget.get("status") == "KNOWN" and type(used) is int and 0 <= used <= 100:
        ttl_html = (f'<div class="v">{used}% thời hạn TTL đã dùng (không phải % code)</div>'
                    f'<div class="meter m-ttl" role="img" aria-label="child TTL used {used}%">'
                    f'<span style="width:{used}%"></span></div>'
                    f'<div class="small">đã dùng {e(str(budget.get("time_used_label")))} / '
                    f'{e(str(budget.get("time_budget_label")))} · còn {e(str(budget.get("remaining_label")))}'
                    f' · hạn {e(str(budget.get("deadline_local")))} · {e(str(budget.get("state")))}'
                    f'{" · QUÁ HẠN" if budget.get("overdue") else ""}</div>')
    else:
        ttl_html = (f'<div class="unknown">UNKNOWN — {e(str(budget.get("reason", "no TTL evidence")))}</div>')

    ms_labels = {"submitted": "Đã gửi (submitted)", "running_verified": "Đang chạy có chứng cứ",
                 "tests_passed": "Tests PASS", "review_passed": "Review PASS", "qa_passed": "QA PASS",
                 "merged": "Merged", "deployed": "Deployed", "accepted": "Nghiệm thu độc lập"}
    ms = "".join(f'<li>{"✅ CÓ" if ok else "⬜ CHƯA"} — {e(ms_labels.get(k, k))}</li>'
                 for k, ok in view.get("milestones", {}).items())
    blockers = view.get("blockers", [])
    reasons = view.get("reasons", [])
    last_ver = next((x for x in reversed(events) if _RANK.get(x.get("provenance_level"), -1)
                     >= _RANK["LOCAL_OBSERVED"]), None)
    last_html = ("không có chứng cứ đã xác minh" if last_ver is None else
                 f'{e(last_ver["kind"])} · {e(last_ver["occurred_at"])} · '
                 f'<span class="tag">{e(last_ver["provenance_level"])}</span>'
                 + (f' <span class="tag">{e(last_ver["sanitized_evidence_ref"])}</span>'
                    if last_ver.get("sanitized_evidence_ref") else ""))
    rows = "".join(
        f'<li><b>{e(x["kind"])}</b>{" · " + e(x["tool_kind"]) if x.get("tool_kind") else ""}'
        f' · {e(x["occurred_at"][11:19])}Z{" · " + e(x["result"]) if x.get("result") else ""}<br>'
        f'<span class="tag">{e(x["provenance_level"])}</span>'
        f'<span class="tag">{e(x["session_id"])}</span></li>'
        for x in events[-30:][::-1])
    bounds = view.get("read_bounds") or {}
    trunc = bounds.get("truncated") or []
    return f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent observability</title><style>{_PAGE_CSS}</style></head><body><main>
<h1>Task #{int(task_id)} · Agent observability</h1>
<p class="sub">DRAFT · chỉ đọc · operator-only. {e(str(view.get("scope_note", "")))}</p>
<section class="card"><h2>Trạng thái (3 chiều độc lập)</h2><div class="grid">
<div><div class="k">Dispatch</div><div class="v">{e(view.get("dispatch", "UNKNOWN"))}</div></div>
<div><div class="k">Execution</div><div class="v {exec_cls}">{e(exec_state)}</div></div>
<div><div class="k">Delivery</div><div class="v">{e(view.get("delivery_stage", "NOT_STARTED"))}</div></div>
<div><div class="k">Độ tươi</div><div class="v {"bad" if fresh.get("stale") else "ok"}">{"STALE → UNKNOWN" if fresh.get("stale") else "FRESH"}</div></div>
</div></section>
<section class="card"><h2>① Hoàn tất đã nghiệm thu (Harness gates)</h2>{comp_html}
<div class="small">Token, heartbeat, số tool call không bao giờ làm tăng %.</div></section>
<section class="card"><h2>② Thời hạn task con (TTL)</h2>{ttl_html}</section>
<section class="card"><h2>Chứng cứ xác minh gần nhất</h2><div>{last_html}</div></section>
<section class="card"><h2>Blockers / lý do</h2>
<div>{"".join(f'<span class="tag bad">{e(b)}</span>' for b in blockers) or '<span class="small">không có blocker từ Harness</span>'}</div>
<div class="small">{e(", ".join(reasons)) or "—"}</div></section>
<section class="card"><h2>Mốc giao hàng</h2><ul>{ms}</ul></section>
<section class="card"><h2>Timeline ({int(view.get("event_count", 0))} sự kiện · {int(view.get("quarantined", 0))} cách ly)</h2>
<ul class="tl">{rows or "<li>chưa có sự kiện</li>"}</ul>
{f'<div class="small warn">Đọc có giới hạn, đã cắt: {e(", ".join(trunc))}</div>' if trunc else ""}</section>
</main></body></html>"""
