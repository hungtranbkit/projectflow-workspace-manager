"""Meta-Monitor P0 (issue #52): evidence ledger, L1/L2/L3 metrics, convergence.

Scope and honesty boundary
--------------------------
* Owner-only, append-only SQLite ledger kept in its OWN file
  (``meta-monitor.db``). It never writes the production Harness DB, never
  changes an automation's prompt, schedule, enabled state, goal, gate policy,
  branch protection or production runtime. It records and evaluates; humans
  (or the monitor prompt) act.
* L1 business outcome comes only from the fresh, gate-based progress
  checkpoint (``harness_monitor_report.report``). Commits, PRs, canaries,
  cron completion and Harness-only WIP are NEVER business progress.
* Missing or stale observations are ``None``/``UNKNOWN`` -- never 0 and never
  an improvement.
* Prompt configuration is not backend enforcement: every report carries
  ``enforcement=LEDGER_ONLY_NOT_BACKEND_ENFORCED`` and
  ``runtime_acceptance=NOT_PROVEN`` until real Meta-Monitor cycles show
  measured improvement.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.secret_redaction import redact  # noqa: E402

DEFAULT_DB = Path.home() / ".local/state/projectflow-harness/meta-monitor.db"
DEFAULT_SNAPSHOT = Path.home() / ".local/state/projectflow-harness/monitor-progress.json"
REPO = Path(__file__).resolve().parents[1]

ENFORCEMENT = "LEDGER_ONLY_NOT_BACKEND_ENFORCED"
FRESH_READBACK_SECONDS = 900
WINDOW = timedelta(days=7)
STABLE_QUIET = timedelta(days=7)
GUARD_MAX_AGE = timedelta(hours=24)
CHECKPOINTS = {"24h": timedelta(hours=24), "72h": timedelta(hours=72),
               "7d": timedelta(days=7)}
VERDICTS = ("KEEP", "REVERT", "INCONCLUSIVE")
MIN_EXPERIMENT_OBSERVATIONS = 2
RATE_MARGIN = 0.10

# Structured blocker codes -> class. Free text is never classified by regex:
# an unlisted code is UNKNOWN, so a product bug cannot silently become an
# orchestration fault (or the reverse).
BLOCKER_CLASSES: dict[str, str] = {
    **{c: "EXTERNAL_OWNER" for c in (
        "OWNER_CONSENT", "OWNER_RIGHTS", "DEVICE_UNAVAILABLE",
        "PLATFORM_PERMISSION", "BILLING", "CREDENTIAL_REQUIRED",
        "LEGAL_LICENSE")},
    **{c: "PRODUCT_DEFECT" for c in (
        "PRODUCT_TEST_FAILURE", "PRODUCT_RUNTIME_BUG", "PRODUCT_SPEC_GAP")},
    **{c: "ORCHESTRATION" for c in (
        "DUPLICATE_DISPATCH", "STALE_READBACK", "CLEANUP_STUCK",
        "TTL_EXPIRED_NO_OUTPUT", "WRONG_TARGET", "MONITOR_CRASH",
        "RECEIPT_MISSING")},
}
# Only these sources may prove an execution/gate delta.
INDEPENDENT_DELTA_SOURCES = {"GATE_ACCEPTANCE", "VERIFIED_TASK_OUTPUT",
                             "INDEPENDENT_TEST"}
# Accepted for the record, but never counted as useful work.
NON_EVIDENCE_DELTA_SOURCES = {"CRON_COMPLETED", "COMMIT", "PR_OPENED",
                              "CANARY", "HARNESS_WIP", "SELF_REPORT"}
READBACK_STATUSES = {"VERIFIED_READBACK", "HARNESS_UNKNOWN", "ABSENT"}
SEVERITIES = ("CRITICAL", "SERIOUS", "MINOR")
DEFECT_KINDS = ("ORCHESTRATION_DEFECT", "REGRESSION")
_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class MetaMonitorError(ValueError):
    """Refused input; the ledger is unchanged."""


# --------------------------------------------------------------------- utils
def aware(value: str | datetime) -> datetime:
    d = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if d.tzinfo is None or d.utcoffset() is None:
        raise MetaMonitorError("timestamp must be timezone-aware")
    return d.astimezone(timezone.utc)


def iso(d: datetime) -> str:
    return aware(d).isoformat()


def ident(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise MetaMonitorError(f"invalid {what}")
    return value


def clean_text(value: Any, what: str, *, limit: int = 500,
               optional: bool = False) -> str | None:
    """No secrets, customer data dumps or pane captures: short, redaction-clean."""
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MetaMonitorError(f"{what} required")
    if len(value) > limit:
        raise MetaMonitorError(f"{what} exceeds {limit} chars")
    if redact(value) != value:
        raise MetaMonitorError(f"{what} looks like it contains a secret")
    return value.strip()


def tri(value: Any, what: str) -> int | None:
    if value is None:
        return None
    if value is True or value is False:
        return int(value)
    raise MetaMonitorError(f"{what} must be true/false/null")


# -------------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_cycle_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id TEXT NOT NULL,
    cycle_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    readback_status TEXT NOT NULL,
    readback_age_seconds INTEGER,
    execution_delta INTEGER,
    gate_delta INTEGER,
    delta_source TEXT,
    delta_evidence TEXT,
    dispatch_key TEXT,
    duplicate_dispatch INTEGER NOT NULL DEFAULT 0,
    blocker_code TEXT,
    blocker_class TEXT,
    blocker_since TEXT,
    owner_intervention INTEGER,
    cleanup_state TEXT,
    cleanup_pending_since TEXT,
    UNIQUE(monitor_id, cycle_at)
);
CREATE TABLE IF NOT EXISTS meta_rule_experiments (
    experiment_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    target_monitor TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    variable TEXT NOT NULL,
    value_before TEXT NOT NULL,
    value_after TEXT NOT NULL,
    prompt_sha256_before TEXT NOT NULL,
    prompt_sha256_after TEXT NOT NULL,
    before_metrics TEXT NOT NULL,
    before_evidence TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    follow_up_due TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(rule_id, rule_version)
);
CREATE TABLE IF NOT EXISTS meta_experiment_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL REFERENCES meta_rule_experiments(experiment_id),
    observed_at TEXT NOT NULL,
    checkpoint TEXT,
    cycle_count INTEGER NOT NULL,
    last_cycle_at TEXT NOT NULL,
    metrics TEXT NOT NULL,
    UNIQUE(experiment_id, observed_at),
    UNIQUE(experiment_id, last_cycle_at)
);
CREATE TABLE IF NOT EXISTS meta_experiment_verdicts (
    experiment_id TEXT PRIMARY KEY REFERENCES meta_rule_experiments(experiment_id),
    verdict TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    rationale TEXT NOT NULL,
    observation_ids TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_defect_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    defect_id TEXT NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('OPEN','RESOLVED')),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    verified INTEGER NOT NULL CHECK(verified IN (0,1)),
    monitor_id TEXT,
    evidence TEXT NOT NULL,
    UNIQUE(defect_id, event)
);
CREATE TABLE IF NOT EXISTS meta_guard_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    suite TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    summary TEXT NOT NULL
);
"""
TABLES = ("meta_cycle_observations", "meta_rule_experiments",
          "meta_experiment_observations", "meta_experiment_verdicts",
          "meta_defect_events", "meta_guard_runs")


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    for table in TABLES:
        for op in ("UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()}
                BEFORE {op} ON {table}
                BEGIN SELECT RAISE(ABORT, 'meta-monitor ledger is append-only'); END""")


@contextmanager
def ledger(path: Path, *, create: bool = False) -> Iterator[sqlite3.Connection]:
    """Owner-only ledger. Refuses group/other-readable files and dirs."""
    path = Path(path)
    if not path.exists():
        if not create:
            raise MetaMonitorError("meta-monitor ledger absent (no observations yet)")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        old = os.umask(0o077)
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        finally:
            os.umask(old)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise MetaMonitorError(f"ledger permissions too open ({oct(mode)}); expected 0600")
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            init_schema(conn)
            yield conn
    finally:
        conn.close()


# ------------------------------------------------------------- write: cycles
def record_cycle(db: sqlite3.Connection, obs: dict[str, Any], now: datetime) -> dict:
    """Append one monitor cycle observation (what the monitor actually saw)."""
    now = aware(now)
    monitor = ident(obs.get("monitor_id"), "monitor_id")
    at = aware(obs.get("cycle_at") or "")
    if at > now + timedelta(seconds=30):
        raise MetaMonitorError("cycle_at is in the future")
    status = obs.get("readback_status")
    if status not in READBACK_STATUSES:
        raise MetaMonitorError("readback_status must be one of "
                               + ",".join(sorted(READBACK_STATUSES)))
    age = obs.get("readback_age_seconds")
    if age is not None and (type(age) is not int or age < -30):
        raise MetaMonitorError("readback_age_seconds must be an int")
    exe, gate = tri(obs.get("execution_delta"), "execution_delta"), tri(
        obs.get("gate_delta"), "gate_delta")
    source = obs.get("delta_source")
    evidence = clean_text(obs.get("delta_evidence"), "delta_evidence",
                          limit=300, optional=True)
    if exe or gate:
        if source not in INDEPENDENT_DELTA_SOURCES | NON_EVIDENCE_DELTA_SOURCES:
            raise MetaMonitorError("a claimed delta needs a known delta_source")
        if source in INDEPENDENT_DELTA_SOURCES and not evidence:
            raise MetaMonitorError("independent delta needs delta_evidence ref")
    elif source is not None and source not in (
            INDEPENDENT_DELTA_SOURCES | NON_EVIDENCE_DELTA_SOURCES):
        raise MetaMonitorError("unknown delta_source")
    dispatch = obs.get("dispatch_key")
    duplicate = 0
    if dispatch is not None:
        dispatch = ident(dispatch, "dispatch_key")
        duplicate = int(db.execute(
            """SELECT 1 FROM meta_cycle_observations WHERE monitor_id=?
               AND dispatch_key=? LIMIT 1""", (monitor, dispatch)).fetchone() is not None)
    code = obs.get("blocker_code")
    klass = since = None
    if code is not None:
        code = ident(code, "blocker_code")
        klass = BLOCKER_CLASSES.get(code, "UNKNOWN")
        since = iso(aware(obs.get("blocker_since") or "")) if obs.get(
            "blocker_since") else None
    cleanup = obs.get("cleanup_state")
    if cleanup not in (None, "NONE", "PENDING"):
        raise MetaMonitorError("cleanup_state must be NONE/PENDING/null")
    pending_since = None
    if cleanup == "PENDING":
        pending_since = iso(aware(obs.get("cleanup_pending_since") or ""))
    try:
        cur = db.execute(
            """INSERT INTO meta_cycle_observations
            (monitor_id,cycle_at,recorded_at,readback_status,readback_age_seconds,
             execution_delta,gate_delta,delta_source,delta_evidence,dispatch_key,
             duplicate_dispatch,blocker_code,blocker_class,blocker_since,
             owner_intervention,cleanup_state,cleanup_pending_since)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (monitor, iso(at), iso(now), status, age, exe, gate, source,
             evidence, dispatch, duplicate, code, klass, since,
             tri(obs.get("owner_intervention"), "owner_intervention"),
             cleanup, pending_since))
    except sqlite3.IntegrityError as exc:
        raise MetaMonitorError("cycle already recorded for this monitor/time") from exc
    return {"id": cur.lastrowid, "duplicate_dispatch": bool(duplicate),
            "blocker_class": klass}


def cycles(db: sqlite3.Connection, monitor: str | None = None) -> list[dict]:
    q = "SELECT * FROM meta_cycle_observations"
    args: tuple = ()
    if monitor is not None:
        q += " WHERE monitor_id=?"
        args = (monitor,)
    return [dict(r) for r in db.execute(q + " ORDER BY cycle_at,id", args)]


# --------------------------------------------------------------- L2 metrics
def _fresh(c: dict) -> bool:
    age = c["readback_age_seconds"]
    return (c["readback_status"] == "VERIFIED_READBACK" and age is not None
            and age <= FRESH_READBACK_SECONDS)


def _useful(c: dict) -> bool:
    return bool(_fresh(c) and not c["duplicate_dispatch"]
                and (c["execution_delta"] == 1 or c["gate_delta"] == 1)
                and c["delta_source"] in INDEPENDENT_DELTA_SOURCES
                and c["delta_evidence"])


def l2_metrics(rows: list[dict], now: datetime, *,
               since: datetime | None = None,
               until: datetime | None = None) -> dict[str, Any]:
    """Monitor effectiveness. Absent evidence => None, never 0."""
    now = aware(now)
    lo = aware(since) if since else now - WINDOW
    hi = aware(until) if until else now
    win = [c for c in rows if lo < aware(c["cycle_at"]) <= hi]
    out: dict[str, Any] = {
        "window_start": iso(lo), "window_end": iso(hi), "cycles": len(win),
        "fresh_cycles": None, "stale_readback_rate": None,
        "useful_action_rate": None, "useful_actions": None,
        "duplicate_dispatch_count": None, "non_evidence_delta_claims": None,
        "no_delta_cycles": None, "owner_interventions": None,
        "blocker": None, "cleanup_pending_age_seconds": None,
    }
    if not win:
        return out
    fresh = [c for c in win if _fresh(c)]
    out["fresh_cycles"] = len(fresh)
    out["stale_readback_rate"] = round(1 - len(fresh) / len(win), 3)
    judged = [c for c in fresh
              if c["execution_delta"] is not None or c["gate_delta"] is not None
              or c["duplicate_dispatch"]]
    if judged:
        useful = sum(_useful(c) for c in judged)
        out["useful_actions"] = useful
        out["useful_action_rate"] = round(useful / len(judged), 3)
    out["duplicate_dispatch_count"] = sum(c["duplicate_dispatch"] for c in win)
    out["non_evidence_delta_claims"] = sum(
        1 for c in win if (c["execution_delta"] == 1 or c["gate_delta"] == 1)
        and c["delta_source"] in NON_EVIDENCE_DELTA_SOURCES)
    # Trailing streak of fresh cycles with an OBSERVED absence of delta.
    streak = 0
    for c in reversed(win):
        if not _fresh(c) or c["execution_delta"] is None or c["gate_delta"] is None:
            break
        if _useful(c):
            break
        streak += 1
    last = win[-1]
    if _fresh(last) and last["execution_delta"] is not None and last["gate_delta"] is not None:
        out["no_delta_cycles"] = streak
    owners = [c["owner_intervention"] for c in win if c["owner_intervention"] is not None]
    out["owner_interventions"] = sum(owners) if owners else None
    if last["blocker_code"]:
        age = None
        if last["blocker_since"]:
            age = max(0, round((hi - aware(last["blocker_since"])).total_seconds()))
        out["blocker"] = {"code": last["blocker_code"], "class": last["blocker_class"],
                          "age_seconds": age}
    if last["cleanup_state"] == "PENDING":
        out["cleanup_pending_age_seconds"] = max(0, round(
            (hi - aware(last["cleanup_pending_since"])).total_seconds()))
    elif last["cleanup_state"] == "NONE":
        out["cleanup_pending_age_seconds"] = 0
    return out


def next_step(metrics: dict[str, Any]) -> dict[str, str]:
    """Course-correction decision. External owner blockers never cause rule churn."""
    blocker = metrics.get("blocker") or {}
    klass = blocker.get("class")
    streak = metrics.get("no_delta_cycles")
    if klass == "EXTERNAL_OWNER":
        return {"action": "HOLD_OWNER_ACTION", "reason":
                f"external blocker {blocker['code']}: one owner action, no pivot"}
    if klass == "PRODUCT_DEFECT":
        return {"action": "FIX_PRODUCT", "reason":
                f"product defect {blocker['code']}: not an orchestration fault"}
    if streak is None:
        return {"action": "UNKNOWN", "reason": "no fresh delta observation"}
    if klass == "UNKNOWN":
        return {"action": "CLASSIFY_BLOCKER", "reason": "unlisted blocker code"}
    if streak >= 3:
        return {"action": "PIVOT", "reason": f"{streak} fresh cycles without delta"}
    if streak >= 2:
        return {"action": "DIAGNOSE", "reason": f"{streak} fresh cycles without delta"}
    return {"action": "CONTINUE", "reason": "recent verified delta"}


# --------------------------------------------------------- L3 experiments
def open_experiment(db: sqlite3.Connection, spec: dict[str, Any],
                    now: datetime) -> dict:
    now = aware(now)
    rule_id = ident(spec.get("rule_id"), "rule_id")
    version = ident(spec.get("rule_version"), "rule_version")
    monitor = ident(spec.get("target_monitor"), "target_monitor")
    diff = spec.get("diff")
    if not isinstance(diff, dict) or len(diff) != 1:
        raise MetaMonitorError("diff must change exactly ONE variable")
    (variable, change), = diff.items()
    ident(variable, "variable")
    if not isinstance(change, dict) or set(change) != {"before", "after"}:
        raise MetaMonitorError("diff variable needs exactly before/after")
    before = clean_text(change["before"], "diff.before", limit=300)
    after = clean_text(change["after"], "diff.after", limit=300)
    if before == after:
        raise MetaMonitorError("diff before/after are identical")
    shas = (spec.get("prompt_sha256_before"), spec.get("prompt_sha256_after"))
    if not all(isinstance(s, str) and _SHA.match(s) for s in shas) or shas[0] == shas[1]:
        raise MetaMonitorError("two distinct prompt sha256 digests required")
    applied = aware(spec.get("applied_at") or "")
    if applied > now + timedelta(seconds=30):
        raise MetaMonitorError("applied_at is in the future")
    history = cycles(db, monitor)
    prior = [c for c in history if aware(c["cycle_at"]) <= applied]
    if prior and prior[-1]["blocker_class"] == "EXTERNAL_OWNER":
        raise MetaMonitorError(
            "EXTERNAL_BLOCKER_NO_RULE_CHURN: target is waiting on an owner action")
    open_rows = db.execute(
        """SELECT e.experiment_id FROM meta_rule_experiments e
           LEFT JOIN meta_experiment_verdicts v USING(experiment_id)
           WHERE e.target_monitor=? AND v.experiment_id IS NULL""", (monitor,)).fetchall()
    if open_rows:
        raise MetaMonitorError(
            f"experiment {open_rows[0][0]} still open on this monitor (one variable at a time)")
    base = l2_metrics(history, applied, until=applied)
    exp_id = f"{rule_id}@{version}"
    due = {k: iso(applied + v) for k, v in CHECKPOINTS.items()}
    try:
        db.execute(
            """INSERT INTO meta_rule_experiments VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (exp_id, rule_id, version, monitor,
             clean_text(spec.get("hypothesis"), "hypothesis"), variable, before, after,
             shas[0], shas[1], json.dumps(base, sort_keys=True),
             clean_text(spec.get("before_evidence"), "before_evidence", limit=300),
             iso(applied), json.dumps(due, sort_keys=True), iso(now)))
    except sqlite3.IntegrityError as exc:
        raise MetaMonitorError("rule_id/version already recorded; use a new version") from exc
    return {"experiment_id": exp_id, "before_metrics": base, "follow_up_due": due}


def _experiment(db: sqlite3.Connection, exp_id: str) -> dict:
    row = db.execute("SELECT * FROM meta_rule_experiments WHERE experiment_id=?",
                     (exp_id,)).fetchone()
    if row is None:
        raise MetaMonitorError("unknown experiment")
    d = dict(row)
    d["before_metrics"] = json.loads(d["before_metrics"])
    d["follow_up_due"] = json.loads(d["follow_up_due"])
    return d


def _observations(db: sqlite3.Connection, exp_id: str) -> list[dict]:
    rows = [dict(r) for r in db.execute(
        """SELECT * FROM meta_experiment_observations WHERE experiment_id=?
           ORDER BY observed_at,id""", (exp_id,))]
    for r in rows:
        r["metrics"] = json.loads(r["metrics"])
    return rows


def observe_experiment(db: sqlite3.Connection, exp_id: str, now: datetime,
                       checkpoint: str | None = None) -> dict:
    """Snapshot after-change metrics from real recorded cycles only."""
    now = aware(now)
    exp = _experiment(db, exp_id)
    if db.execute("SELECT 1 FROM meta_experiment_verdicts WHERE experiment_id=?",
                  (exp_id,)).fetchone():
        raise MetaMonitorError("experiment already judged")
    applied = aware(exp["applied_at"])
    if checkpoint is not None:
        if checkpoint not in CHECKPOINTS:
            raise MetaMonitorError("checkpoint must be 24h/72h/7d")
        if now < applied + CHECKPOINTS[checkpoint]:
            raise MetaMonitorError(f"{checkpoint} checkpoint not reached; no sample fabricated")
    after = [c for c in cycles(db, exp["target_monitor"])
             if applied < aware(c["cycle_at"]) <= now]
    if not after:
        raise MetaMonitorError("NO_NEW_CYCLE_EVIDENCE since the rule change")
    last_cycle = after[-1]["cycle_at"]
    previous = _observations(db, exp_id)
    if previous and aware(previous[-1]["last_cycle_at"]) >= aware(last_cycle):
        raise MetaMonitorError("NO_NEW_CYCLE_EVIDENCE since the previous observation")
    metrics = l2_metrics(after, now, since=applied, until=now)
    cur = db.execute(
        """INSERT INTO meta_experiment_observations
           (experiment_id,observed_at,checkpoint,cycle_count,last_cycle_at,metrics)
           VALUES (?,?,?,?,?,?)""",
        (exp_id, iso(now), checkpoint, len(after), last_cycle,
         json.dumps(metrics, sort_keys=True)))
    return {"id": cur.lastrowid, "metrics": metrics}


def evaluate_experiment(db: sqlite3.Connection, exp_id: str, now: datetime) -> dict:
    now = aware(now)
    exp = _experiment(db, exp_id)
    obs = _observations(db, exp_id)
    verdict = db.execute("SELECT * FROM meta_experiment_verdicts WHERE experiment_id=?",
                         (exp_id,)).fetchone()
    taken = {o["checkpoint"] for o in obs if o["checkpoint"]}
    checkpoints = {}
    for name, due in exp["follow_up_due"].items():
        if name in taken:
            checkpoints[name] = "SAMPLED"
        elif now >= aware(due):
            checkpoints[name] = "OVERDUE_NO_SAMPLE"
        else:
            checkpoints[name] = "NOT_DUE"
    before = exp["before_metrics"].get("useful_action_rate")
    recommendation, reason = "INCONCLUSIVE", None
    if len(obs) < MIN_EXPERIMENT_OBSERVATIONS:
        status = "AWAITING_OBSERVATIONS"
        reason = f"{len(obs)}/{MIN_EXPERIMENT_OBSERVATIONS} new observations"
    else:
        status = "JUDGEABLE"
        rates = [o["metrics"].get("useful_action_rate") for o in obs[-2:]]
        if before is None or any(r is None for r in rates):
            reason = "useful_action_rate UNKNOWN before or after the change"
        else:
            # Both latest observations must agree in direction.
            if all(r >= before + RATE_MARGIN for r in rates):
                recommendation, reason = "KEEP", f"rate {before} -> {rates}"
            elif all(r <= before - RATE_MARGIN for r in rates):
                recommendation, reason = "REVERT", f"rate {before} -> {rates}"
            else:
                reason = f"rate {before} -> {rates}: within margin or mixed"
    return {"experiment_id": exp_id, "target_monitor": exp["target_monitor"],
            "variable": exp["variable"], "status": "JUDGED" if verdict else status,
            "observations": len(obs), "checkpoints": checkpoints,
            "before_useful_action_rate": before,
            "recommendation": recommendation, "reason": reason,
            "verdict": verdict["verdict"] if verdict else None}


def record_verdict(db: sqlite3.Connection, exp_id: str, verdict: str,
                   rationale: str, now: datetime) -> dict:
    if verdict not in VERDICTS:
        raise MetaMonitorError("verdict must be KEEP/REVERT/INCONCLUSIVE")
    ev = evaluate_experiment(db, exp_id, now)
    if ev["verdict"]:
        raise MetaMonitorError("experiment already judged; open a new rule version")
    if verdict != "INCONCLUSIVE":
        if ev["status"] != "JUDGEABLE":
            raise MetaMonitorError(f"cannot judge {verdict}: {ev['reason']}")
        if ev["recommendation"] != verdict:
            raise MetaMonitorError(
                f"evidence recommends {ev['recommendation']}, not {verdict}")
    ids = [o["id"] for o in _observations(db, exp_id)]
    db.execute("INSERT INTO meta_experiment_verdicts VALUES (?,?,?,?,?)",
               (exp_id, verdict, iso(now), clean_text(rationale, "rationale"),
                json.dumps(ids)))
    return {"experiment_id": exp_id, "verdict": verdict, "observation_ids": ids}


def rollback_proposal(db: sqlite3.Connection, exp_id: str,
                      current_prompt_sha256: str | None, now: datetime) -> dict:
    """Propose (never apply) a rollback; refuse over a concurrent prompt edit."""
    exp = _experiment(db, exp_id)
    ev = evaluate_experiment(db, exp_id, now)
    if "REVERT" not in (ev["verdict"], ev["recommendation"]):
        return {"action": "NONE", "reason": "no measured regression for this rule"}
    if current_prompt_sha256 is None:
        return {"action": "UNKNOWN", "reason": "read the live prompt digest first"}
    if current_prompt_sha256 != exp["prompt_sha256_after"]:
        return {"action": "CONCURRENT_EDIT_DETECTED",
                "reason": "live prompt differs from the experiment's after-digest; "
                          "manual merge required, never overwrite",
                "expected_sha256": exp["prompt_sha256_after"],
                "observed_sha256": current_prompt_sha256}
    return {"action": "PROPOSE_ROLLBACK", "experiment_id": exp_id,
            "target_monitor": exp["target_monitor"], "variable": exp["variable"],
            "restore_value": exp["value_before"], "replace_value": exp["value_after"],
            "precondition_prompt_sha256": exp["prompt_sha256_after"],
            "applies_automatically": False}


def l3_summary(db: sqlite3.Connection, now: datetime) -> dict:
    ids = [r[0] for r in db.execute(
        "SELECT experiment_id FROM meta_rule_experiments ORDER BY applied_at")]
    evals = [evaluate_experiment(db, i, now) for i in ids]
    counts = {v: sum(e["verdict"] == v for e in evals) for v in VERDICTS}
    judged = sum(counts.values())
    return {"experiments": len(evals), "open": len(evals) - judged,
            "verdicts": counts,
            "revert_rate": round(counts["REVERT"] / judged, 3) if judged else None,
            "overdue_checkpoints": sum(
                list(e["checkpoints"].values()).count("OVERDUE_NO_SAMPLE") for e in evals),
            "items": evals[-5:]}


# ------------------------------------------------- defects, guards, convergence
def record_defect(db: sqlite3.Connection, ev: dict[str, Any], now: datetime) -> None:
    event = ev.get("event")
    if event not in ("OPEN", "RESOLVED"):
        raise MetaMonitorError("event must be OPEN/RESOLVED")
    kind, sev = ev.get("kind"), ev.get("severity")
    if kind not in DEFECT_KINDS or sev not in SEVERITIES:
        raise MetaMonitorError("invalid defect kind/severity")
    try:
        db.execute(
            """INSERT INTO meta_defect_events
               (defect_id,event,at,kind,severity,verified,monitor_id,evidence)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ident(ev.get("defect_id"), "defect_id"), event,
             iso(aware(ev.get("at") or iso(now))), kind, sev,
             int(ev.get("verified") is True),
             ident(ev["monitor_id"], "monitor_id") if ev.get("monitor_id") else None,
             clean_text(ev.get("evidence"), "evidence", limit=300)))
    except sqlite3.IntegrityError as exc:
        raise MetaMonitorError("defect event already recorded") from exc


def record_guard(db: sqlite3.Connection, suite: str, commit: str, exit_code: int,
                 summary: str, now: datetime) -> None:
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
        raise MetaMonitorError("guard run needs the tested commit sha")
    if type(exit_code) is not int:
        raise MetaMonitorError("exit_code must be int")
    db.execute("INSERT INTO meta_guard_runs (at,suite,commit_sha,exit_code,summary)"
               " VALUES (?,?,?,?,?)", (iso(now), ident(suite, "suite"), commit,
                                       exit_code, clean_text(summary, "summary", limit=200)))


def open_defects(db: sqlite3.Connection) -> list[dict]:
    rows = [dict(r) for r in db.execute("SELECT * FROM meta_defect_events ORDER BY at,id")]
    resolved = {r["defect_id"] for r in rows if r["event"] == "RESOLVED"}
    return [r for r in rows if r["event"] == "OPEN" and r["defect_id"] not in resolved]


def convergence(db: sqlite3.Connection, now: datetime) -> dict:
    """STABLE_MAINTENANCE only with measured evidence, green guards, quiet 7d."""
    now = aware(now)
    reasons: list[str] = []
    open_ = open_defects(db)
    regressions = [d for d in open_ if d["kind"] == "REGRESSION" and d["verified"]]
    if regressions:
        return {"state": "REGRESSION_ACTIVE",
                "reasons": [f"verified regression {d['defect_id']} unresolved"
                            for d in regressions]}
    rows = cycles(db)
    fresh = [c for c in rows if _fresh(c)]
    if (not fresh or aware(fresh[0]["cycle_at"]) > now - STABLE_QUIET
            or aware(fresh[-1]["cycle_at"]) < now - timedelta(hours=24)):
        return {"state": "UNKNOWN",
                "reasons": ["INSUFFICIENT_EVIDENCE: need fresh cycles spanning 7d up to the last 24h"]}
    if l2_metrics(rows, now, since=now - STABLE_QUIET)["useful_action_rate"] is None:
        return {"state": "UNKNOWN", "reasons": ["INSUFFICIENT_EVIDENCE: no measured action outcome"]}
    guard = db.execute("SELECT * FROM meta_guard_runs ORDER BY at DESC,id DESC LIMIT 1").fetchone()
    if guard is None:
        reasons.append("GUARD_EVIDENCE_ABSENT")
    elif guard["exit_code"] != 0:
        reasons.append(f"GUARD_FAILING exit={guard['exit_code']}@{guard['commit_sha'][:12]}")
    elif aware(guard["at"]) < now - GUARD_MAX_AGE:
        reasons.append("GUARD_EVIDENCE_STALE")
    for d in open_:
        if d["severity"] in ("CRITICAL", "SERIOUS"):
            reasons.append(f"UNRESOLVED_{d['severity']} {d['defect_id']}")
    recent = [r for r in db.execute(
        "SELECT * FROM meta_defect_events WHERE event='OPEN' AND kind='ORCHESTRATION_DEFECT'")
        if aware(r["at"]) > now - STABLE_QUIET]
    if recent:
        reasons.append(f"{len(recent)} orchestration defect(s) opened in the last 7d")
    orch = [c for c in rows if c["blocker_class"] == "ORCHESTRATION"
            and aware(c["cycle_at"]) > now - STABLE_QUIET]
    if orch:
        reasons.append(f"{len(orch)} cycle(s) with orchestration blockers in the last 7d")
    unverified = [d["defect_id"] for d in open_ if d["kind"] == "REGRESSION" and not d["verified"]]
    out = {"state": "ACTIVE_IMPROVEMENT" if reasons else "STABLE_MAINTENANCE",
           "reasons": reasons or ["7d quiet, guards green, measured evidence present"]}
    if unverified:
        out["unverified_regression_claims"] = unverified
    return out


# --------------------------------------------------------------------- L1
def l1_outcome(snapshot: dict | None, monitor: str, now: datetime) -> dict:
    """Business outcome from the gate checkpoint only; stale => UNKNOWN."""
    from scripts.harness_monitor_report import report
    if snapshot is None:
        return {"status": "UNKNOWN", "reason": "progress checkpoint absent"}
    try:
        r = report(snapshot, monitor, aware(now))
    except (KeyError, ValueError, TypeError) as exc:
        return {"status": "UNKNOWN", "reason": f"{type(exc).__name__}"}
    if r["status"] != "VERIFIED_READBACK":
        return {"status": "UNKNOWN", "reason": r.get("reason")}
    pts = [p for p in ((r.get("chart") or {}).get("actual") or [])
           if aware(p["at"]) > aware(now) - WINDOW]
    return {"status": "MEASURED", "completion_percent": r["completion_percent"],
            "gates": f'{r["gates_passed"]}/{r["gates_total"]}',
            "gate_delta_pp_7d": (pts[-1]["percent"] - pts[0]["percent"])
            if len(pts) >= 2 else None,
            "business_accepted": r["project_business_accepted"]}


def meta_report(db_path: Path, snapshot: dict | None, now: datetime,
                monitors: list[str] | None = None) -> dict:
    now = aware(now)
    base = {"schema": "meta-monitor-v1", "as_of": iso(now), "enforcement": ENFORCEMENT,
            "runtime_acceptance": "NOT_PROVEN"}
    if not Path(db_path).exists():
        return {**base, "status": "UNKNOWN", "reason": "ledger absent: no observations yet"}
    with ledger(db_path) as db:
        ids = monitors or sorted({c["monitor_id"] for c in cycles(db)})
        per = {}
        for m in ids:
            l2 = l2_metrics(cycles(db, m), now)
            per[m] = {"l1": l1_outcome(snapshot, m, now), "l2": l2, "next": next_step(l2)}
        return {**base, "status": "OK", "monitors": per,
                "l3": l3_summary(db, now), "convergence": convergence(db, now)}


# --------------------------------------------------------------------- CLI
def _git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
                          capture_output=True, text=True, shell=False).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("cycle-record", "experiment-open", "defect-record"):
        sub.add_parser(name).add_argument("--json", required=True)
    o = sub.add_parser("experiment-observe")
    o.add_argument("--experiment-id", required=True)
    o.add_argument("--checkpoint", choices=sorted(CHECKPOINTS))
    v = sub.add_parser("experiment-verdict")
    v.add_argument("--experiment-id", required=True)
    v.add_argument("--verdict", choices=VERDICTS, required=True)
    v.add_argument("--rationale", required=True)
    rb = sub.add_parser("rollback-check")
    rb.add_argument("--experiment-id", required=True)
    rb.add_argument("--current-prompt-sha256")
    sub.add_parser("guard-run", help="run the local guard suite and record its exit code")
    r = sub.add_parser("report")
    r.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    r.add_argument("--monitor", action="append")
    args = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    db_path = Path(args.db)
    try:
        if args.cmd == "report":
            snap_path = Path(args.snapshot)
            snap = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.is_file() else None
            out = meta_report(db_path, snap, now, args.monitor)
        elif args.cmd == "guard-run":
            proc = subprocess.run([str(REPO / "scripts/ci-test.sh"), "fast",
                                   "tests/test_harness_meta_monitor.py"],
                                  cwd=REPO, shell=False, capture_output=True, text=True,
                                  env={**os.environ, "PYTHON": sys.executable})
            tail = (proc.stdout.strip().splitlines() or ["no output"])[-1][:200]
            with ledger(db_path, create=True) as db:
                record_guard(db, "ci-test-fast+meta", _git_head(), proc.returncode, tail, now)
            out = {"guard_exit_code": proc.returncode, "summary": tail}
        else:
            with ledger(db_path, create=True) as db:
                if args.cmd == "cycle-record":
                    out = record_cycle(db, json.loads(args.json), now)
                elif args.cmd == "experiment-open":
                    out = open_experiment(db, json.loads(args.json), now)
                elif args.cmd == "defect-record":
                    record_defect(db, json.loads(args.json), now)
                    out = {"recorded": True}
                elif args.cmd == "experiment-observe":
                    out = observe_experiment(db, args.experiment_id, now, args.checkpoint)
                elif args.cmd == "experiment-verdict":
                    out = record_verdict(db, args.experiment_id, args.verdict,
                                         args.rationale, now)
                else:
                    out = rollback_proposal(db, args.experiment_id,
                                            args.current_prompt_sha256, now)
    except (MetaMonitorError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        print("META_MONITOR=" + json.dumps({"status": "REFUSED", "reason": str(exc)[:200]},
                                           ensure_ascii=False))
        return 2
    print("META_MONITOR=" + json.dumps(out, ensure_ascii=False, separators=(",", ":"),
                                       default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
