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
            "observed_at")
IDENTITY = ("node_id", "project_id", "task_id", "session_id", "agent_id")

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

    def content_hash(self) -> str:
        body = {k: v for k, v in asdict(self).items() if k not in ("observed_at",)}
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
            self._bump(db, "accepted")
            if late:
                self._bump(db, "late_arrivals")
            # Bounded growth: drop oldest events; dedupe ledger is kept far
            # longer so a replay of a pruned event is still recognized.
            db.execute("DELETE FROM agent_obs_events WHERE seq <= ? - ?", (seq, self.max_events))
            db.execute("DELETE FROM agent_obs_dedupe WHERE first_seq <= ? - ?", (seq, self.max_dedupe))
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


def derive_task_view(events: list[dict[str, Any]], *, at: datetime,
                     policy: FreshnessPolicy = FreshnessPolicy()) -> dict[str, Any]:
    """Pure function. Activity volume never touches completion; completion is
    attached separately from Harness (attach_harness)."""
    at = _aware(at)
    evs = sorted(events, key=_key)
    reasons: list[str] = []

    # Dispatch dimension
    dispatch = "QUEUED"
    if any(e["kind"] in DISPATCH_KINDS - {"task_queued"} for e in evs):
        dispatch = "DISPATCHED_UNVERIFIED"
    if any((e["kind"] in LIVENESS_KINDS | PROGRESS_KINDS) and _rank(e) >= _RANK["LOCAL_OBSERVED"]
           for e in evs):
        dispatch = "ACKNOWLEDGED"

    # Execution dimension: only LOCAL_OBSERVED+ may certify a live state.
    # CENTRAL_ATTESTED / self-reports may only push toward UNKNOWN/negative.
    verified = [e for e in evs if e["kind"] in EXEC_KINDS and _rank(e) >= _RANK["LOCAL_OBSERVED"]]
    negative_attested = [e for e in evs if e["kind"] in EXEC_TERMINAL_KINDS | {"waiting_input", "node_disconnected"}
                         and _rank(e) == _RANK["CENTRAL_ATTESTED"]]
    last_any = max((_key(e)[0] for e in evs), default=None)
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

    # Delivery milestones: each independent; FAIL never counts; a later FAIL
    # of the same milestone supersedes an older PASS.
    milestones = {"submitted": dispatch != "QUEUED",
                  "running_verified": execution == "RUNNING_VERIFIED"}
    for kind, (name, minimum) in MILESTONE_KINDS.items():
        rel = [e for e in evs if e["kind"] == kind and _rank(e) >= _RANK[minimum]
               and e.get("result") in {"PASS", "FAIL", "ERROR"}]
        milestones[name] = bool(rel) and rel[-1]["result"] == "PASS"
        ignored = [e for e in evs if e["kind"] == kind and _rank(e) < _RANK[minimum]]
        if ignored:
            reasons.append(f"{name}_claim_below_required_provenance")
    stage = "NOT_STARTED"
    if dispatch == "ACKNOWLEDGED":
        stage = "CODING"
    for st, m in (("TESTING", "tests_passed"), ("REVIEW", "review_passed"),
                  ("QA", "qa_passed"), ("MERGED", "merged"),
                  ("DEPLOYED", "deployed"), ("ACCEPTED", "accepted")):
        if milestones[m]:
            stage = st
    age = (at - last_any).total_seconds() if last_any else None
    return {
        "dispatch": dispatch,
        "execution": execution,
        "delivery_stage": stage,
        "milestones": milestones,
        "reasons": reasons,
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
# T009 — reuse Harness gate completion + immutable child TTL (no new math)
# --------------------------------------------------------------------------

def attach_harness(view: dict[str, Any], *, snapshot: dict[str, Any] | None,
                   automation_id: str | None, harness_task_id: int | None = None,
                   at: datetime | None = None, health_max_age: int = 900) -> dict[str, Any]:
    """Copy (never recompute) completion % and child TTL from the existing
    scripts/harness_progress.summarize() snapshot. Stale/absent snapshot or
    unhealthy service -> UNKNOWN, never the last green value."""
    out = dict(view)
    out["blockers"] = list(view.get("blockers", []))
    if snapshot is None or automation_id is None:
        out["completion_percent"] = None
        out["completion_source"] = "UNKNOWN: Harness snapshot unavailable"
        return out
    health = snapshot.get("health") or {}
    try:
        snap_age = (_aware(at or datetime.now(timezone.utc)) - _aware(snapshot["observed_at"])).total_seconds()
    except (KeyError, ObservabilityError, ValueError):
        snap_age = None
    monitor = next((m for m in snapshot.get("monitors", []) if m.get("id") == automation_id), None)
    if snap_age is None or snap_age > health_max_age or snap_age < -300 or health.get("status") != "HEALTHY":
        out["completion_percent"] = None
        out["completion_source"] = "UNKNOWN: Harness snapshot stale or service not HEALTHY"
        out["blockers"].append("HARNESS_EVIDENCE_STALE")
    elif monitor is None:
        out["completion_percent"] = None
        out["completion_source"] = "UNKNOWN: automation not in Harness snapshot"
    else:
        out["completion_percent"] = monitor.get("completion_percent")
        out["completion_source"] = ("harness_progress: PASSED evidence gates only "
                                    f"({monitor.get('gates_passed')}/{monitor.get('gates_total')})"
                                    if monitor.get("completion_percent") is not None
                                    else "N/A: maintenance monitor has no completion %")
    if monitor is not None and harness_task_id is not None:
        clock = next((t for t in monitor.get("active_tasks", []) + monitor.get("recent_tasks", [])
                      if t.get("id") == harness_task_id), None)
        if clock is not None:
            out["time_budget"] = {k: clock.get(k) for k in (
                "state", "cleanup", "time_used_percent", "time_used_label",
                "time_budget_label", "remaining_label", "deadline_local", "overdue", "finished")}
            if clock.get("cleanup") == "CLEANUP_PENDING" or clock.get("state") == "EXPIRED" or clock.get("overdue"):
                out["blockers"].append("CLEANUP_PENDING" if clock.get("cleanup") == "CLEANUP_PENDING"
                                       else "TTL_EXPIRED")
                if out.get("execution") == "RUNNING_VERIFIED":
                    out["execution"] = "UNKNOWN"
                    out["reasons"] = list(out.get("reasons", [])) + ["ttl_expired_overrides_running"]
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


def projectflow_session_events(db_path: str | Path, *, node_id: str, project_id: str,
                               limit: int = 500, task_id: int | None = None) -> list[dict[str, Any]]:
    """Read-only (sqlite mode=ro) projection of agent_sessions + evidence
    tables into raw events. Never selects cwd, transcript_tail, prompts,
    stdout/stderr, findings or notes. Event ids are stable so re-reading the
    same rows is a no-op replay."""
    out: list[dict[str, Any]] = []
    base = {"schema_version": SCHEMA_VERSION, "node_id": node_id, "project_id": project_id}
    db = _ro_connect(db_path)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sessions = db.execute(
            """SELECT id,task_id,workspace_id,agent,status,started_at,last_activity_at,
                      exited_at,exit_code FROM agent_sessions
               WHERE task_id IS NOT NULL AND (? IS NULL OR task_id=?)
               ORDER BY id DESC LIMIT ?""", (task_id, task_id, limit)).fetchall()
        for s in sessions:
            ident = {**base, "task_id": f"pf-task-{s['task_id']}",
                     "session_id": f"pf-session-{s['id']}",
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
        # Evidence rows are bound to the latest session of the same task.
        latest = {}
        for s in sessions:
            latest.setdefault(s["task_id"], s)

        def evidence(kind: str, table: str, rid: int, task_id: int, status: str, at: str | None,
                     pass_values: set[str], fail_values: set[str]) -> None:
            s = latest.get(task_id)  # also enforces the optional task filter
            if s is None or not at:
                return
            result = "PASS" if status in pass_values else "FAIL" if status in fail_values else "PENDING"
            out.append({**base, "task_id": f"pf-task-{task_id}",
                        "session_id": f"pf-session-{s['id']}", "agent_id": f"{s['agent']}-{s['id']}",
                        "source": "projectflow_evidence", "provenance_level": "LOCAL_VERIFIED",
                        "event_id": f"pf-{table}-{rid}-{status}", "kind": kind, "result": result,
                        "sanitized_evidence_ref": f"{table}:{rid}", "occurred_at": _ts(at)})
        if "review_runs" in tables:
            for r in db.execute("SELECT id,task_id,status,completed_at FROM review_runs WHERE task_id IS NOT NULL"):
                evidence("review_result", "review_runs", r["id"], r["task_id"], r["status"],
                         r["completed_at"], {"PASS"}, {"FIX_REQUIRED", "FAIL"})
        if "qa_runs" in tables:
            for r in db.execute("SELECT id,task_id,status,completed_at FROM qa_runs"):
                evidence("qa_result", "qa_runs", r["id"], r["task_id"], r["status"],
                         r["completed_at"], {"PASS"}, {"FAIL"})
        if "merge_records" in tables:
            for r in db.execute("SELECT id,task_id,merge_status,merged_at FROM merge_records"):
                evidence("merge_recorded", "merge_records", r["id"], r["task_id"], r["merge_status"],
                         r["merged_at"], {"MERGED"}, set())
        if "deployments" in tables:
            for r in db.execute("SELECT id,task_id,status,finished_at FROM deployments WHERE task_id IS NOT NULL"):
                evidence("deploy_recorded", "deployments", r["id"], r["task_id"], r["status"],
                         r["finished_at"], {"VERIFIED"}, {"FAILED", "ROLLED_BACK"})
        if "test_runs" in tables:
            ws_task = {s["workspace_id"]: s["task_id"] for s in sessions}
            for r in db.execute("""SELECT id,workspace_id,status,finished_at FROM test_runs
                                   WHERE workspace_type='agent'"""):
                if r["workspace_id"] in ws_task:
                    evidence("test_result", "test_runs", r["id"], ws_task[r["workspace_id"]],
                             r["status"], r["finished_at"], {"PASSED", "PASS"}, {"FAILED", "FAIL", "ERROR"})
    finally:
        db.close()
    return out


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


def view_from_raw(raws: Iterable[Any], *, at: datetime,
                  policy: FreshnessPolicy = FreshnessPolicy()) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """In-memory normalize + dedupe + derive, with NO persistence. Used by
    GET routes so a read never writes (spec plan: no mutation from GET).
    Returns (view, ordered events, quarantined_count)."""
    seen: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    quarantined = 0
    for i, raw in enumerate(raws, start=1):
        item = normalize(raw, observed_at=at)
        if isinstance(item, Quarantine):
            quarantined += 1
            continue
        h = item.content_hash()
        if item.dedupe_key in seen:
            quarantined += seen[item.dedupe_key] != h
            continue
        seen[item.dedupe_key] = h
        events.append({**item.to_public(), "seq": i, "late_arrival": False})
    events.sort(key=_key)
    view = derive_task_view(events, at=at, policy=policy)
    view["quarantined"] = quarantined
    return view, events, quarantined
