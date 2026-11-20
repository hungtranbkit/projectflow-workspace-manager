"""Agent observability P0 security hardening (PR #56 independent QA blockers).

1. Snapshot / link / registry reads refuse ANY symlink component (direct
   or parent), have no check-then-open race, are size bounded, and fail
   closed on malicious content.
2. attach_harness never raises on malformed snapshot/monitor/clock input;
   it returns UNKNOWN + HARNESS_EVIDENCE_MALFORMED.
3. No unvalidated snapshot string (token, private path, raw log) is ever
   copied into the operator JSON/HTML.
5. Upgrading a pre-ledger event store backfills agent_obs_event_ids
   atomically and idempotently; legacy event_id collisions are
   quarantined, never reassigned.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3

import pytest

from app.services import agent_observability as obs

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
TOKEN = "ghp_" + "S3cr3tT0k3nV4lu3AbCdEf12"
PRIVATE = "/home/rag/.ssh/id_rsa"
AID = "AUTO_SECURITY_1"


def snapshot(**monitor_over):
    clock = {"id": 3, "state": "RUNNING_VERIFIED", "cleanup": "NOT_STARTED", "time_used_percent": 40,
             "time_used_label": "24m 00s", "time_budget_label": "1h 00m", "remaining_label": "36m 00s",
             "deadline_local": "20/11 19:36", "overdue": False, "finished": False}
    clock.update(monitor_over.pop("clock", {}))
    mon = {"id": AID, "kind": "project", "completion_percent": 50, "gates_passed": 3, "gates_total": 6,
           "active_tasks": [clock], "recent_tasks": []}
    mon.update(monitor_over)
    return {"schema": obs.HARNESS_SNAPSHOT_SCHEMA, "observed_at": NOW.isoformat(),
            "health": {"status": "HEALTHY"}, "monitors": [mon]}


BASE_VIEW = {"execution": "RUNNING_VERIFIED", "blockers": [], "reasons": []}


def attach(snap, tid=3):
    return obs.attach_harness(dict(BASE_VIEW), snapshot=snap, automation_id=AID,
                              harness_task_id=tid, at=NOW)


def test_valid_snapshot_still_known():
    out = attach(snapshot())
    assert out["completion_percent"] == 50 and out["completion_source"].endswith("(3/6)")
    assert out["time_budget"]["status"] == "KNOWN" and out["time_budget"]["remaining_label"] == "36m 00s"
    assert out["blockers"] == []


# ---------------------------------------------------------------- 2. malformed -> UNKNOWN, no exception

MALFORMED = [
    lambda s: s.update(health="bad"),
    lambda s: s.update(health=["HEALTHY"]),
    lambda s: s.update(health={"status": ["HEALTHY"]}),
    lambda s: s.update(health={"status": "HEALTHY-ish"}),
    lambda s: s.update(monitors="x"),
    lambda s: s.update(monitors={"id": AID}),
    lambda s: s["monitors"].append(dict(s["monitors"][0])),  # ambiguous duplicate id
    lambda s: s["monitors"][0].update(kind=["project"]),
    lambda s: s["monitors"][0].update(completion_percent=True),
    lambda s: s["monitors"][0].update(completion_percent=101),
    lambda s: s["monitors"][0].update(completion_percent="50"),
    lambda s: s["monitors"][0].update(active_tasks="nope"),
    lambda s: s["monitors"][0].update(recent_tasks={"id": 3}),
    lambda s: s["monitors"][0].update(gates_total=0),
    lambda s: s["monitors"][0].update(gates_passed=7),
    lambda s: s["monitors"][0].update(kind="maintenance"),  # maintenance with a percent
]


@pytest.mark.parametrize("mutate", MALFORMED)
def test_malformed_snapshot_is_unknown_never_raises(mutate):
    snap = snapshot()
    mutate(snap)
    out = attach(snap)
    assert out["completion_percent"] is None
    assert out["completion_source"].startswith("UNKNOWN")
    assert "HARNESS_EVIDENCE_MALFORMED" in out["blockers"]
    assert out["time_budget"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("snap", ["bad", 5, [], None.__class__, {"health": None}])
def test_non_mapping_snapshot_is_unknown(snap):
    out = obs.attach_harness(dict(BASE_VIEW), snapshot=snap, automation_id=AID, at=NOW)
    assert out["completion_percent"] is None and "HARNESS_EVIDENCE_MALFORMED" in out["blockers"]


@pytest.mark.parametrize("observed", [12345, ["2026"], "not-a-date", "2026-11-20T12:00:00", "9" * 500])
def test_malformed_timestamp_is_stale_unknown(observed):
    snap = snapshot()
    snap["observed_at"] = observed
    out = attach(snap)
    assert out["completion_percent"] is None and "HARNESS_EVIDENCE_STALE" in out["blockers"]


@pytest.mark.parametrize("clock", [
    {"state": "PWNED"}, {"cleanup": "rm -rf"}, {"overdue": "no"}, {"finished": 0},
    {"time_used_percent": 140}, {"time_used_percent": "40"}, {"time_used_percent": None},
])
def test_malformed_clock_is_unknown_and_blocks_running(clock):
    out = attach(snapshot(clock=clock))
    assert out["time_budget"]["status"] == "UNKNOWN"
    assert "HARNESS_EVIDENCE_MALFORMED" in out["blockers"]
    assert out["execution"] == "UNKNOWN"


def test_bool_child_id_does_not_match_int_link():
    out = attach(snapshot(clock={"id": True}), tid=1)
    assert out["time_budget"]["status"] == "UNKNOWN"


# ---------------------------------------------------------------- 3. no leak of snapshot strings

@pytest.mark.parametrize("over", [
    {"gates_passed": TOKEN}, {"gates_total": PRIVATE}, {"gates_passed": 2.5},
    {"clock": {"time_used_label": TOKEN}}, {"clock": {"time_budget_label": PRIVATE}},
    {"clock": {"remaining_label": "password=hunter2"}}, {"clock": {"deadline_local": PRIVATE}},
    {"clock": {"state": TOKEN}}, {"clock": {"cleanup": PRIVATE}},
    {"clock": {"remaining_label": "1h 00m\n" + TOKEN}},
])
def test_fabricated_secrets_in_snapshot_never_leak(over):
    out = attach(snapshot(**over))
    blob = json.dumps(out)
    html = obs.render_operator_page(7, out, [])
    for leaked in (TOKEN, PRIVATE, "hunter2"):
        assert leaked not in blob and leaked not in html
    assert "HARNESS_EVIDENCE_MALFORMED" in out["blockers"]
    assert out["completion_percent"] is None or out["time_budget"]["status"] == "UNKNOWN"


def test_extra_monitor_and_clock_fields_are_never_copied():
    snap = snapshot(clock={"name": TOKEN, "stdout": PRIVATE}, prompt=TOKEN, raw_log=PRIVATE)
    out = attach(snap)
    assert out["time_budget"]["status"] == "KNOWN"
    assert TOKEN not in json.dumps(out) and PRIVATE not in json.dumps(out)


# ---------------------------------------------------------------- 1. safe file reads

def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


def test_regular_file_reads(tmp_path):
    p = _write(tmp_path / "state" / "snap.json", snapshot())
    data, why = obs.load_harness_snapshot(p)
    assert why == "ok" and data["monitors"][0]["id"] == AID


def test_direct_symlink_refused(tmp_path):
    real = _write(tmp_path / "secret.json", snapshot())
    link = tmp_path / "snap.json"
    link.symlink_to(real)
    assert obs.load_harness_snapshot(link) == (None, "Harness progress snapshot unreadable or malformed")


def test_parent_directory_symlink_refused(tmp_path):
    _write(tmp_path / "elsewhere" / "snap.json", snapshot())
    (tmp_path / "state").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    data, why = obs.load_harness_snapshot(tmp_path / "state" / "snap.json")
    assert data is None and why == "Harness progress snapshot unreadable or malformed"
    # the same file through the real path is fine (refusal is about the link)
    assert obs.load_harness_snapshot(tmp_path / "elsewhere" / "snap.json")[1] == "ok"


def test_dotdot_and_relative_paths_refused(tmp_path):
    _write(tmp_path / "a" / "snap.json", snapshot())
    for bad in (str(tmp_path / "b" / ".." / "a" / "snap.json"), "a/snap.json"):
        with pytest.raises(obs.ObservabilityError):
            obs._read_small_bytes(bad, 1024)


def test_outside_trusted_root_refused(tmp_path):
    p = _write(tmp_path / "x" / "snap.json", snapshot())
    with pytest.raises(obs.ObservabilityError):
        obs._read_small_bytes(p, 1 << 20, root=tmp_path / "y")
    assert obs._read_small_bytes(p, 1 << 20, root=tmp_path)


def test_replacement_race_between_walk_and_open_is_refused(tmp_path, monkeypatch):
    target = _write(tmp_path / "state" / "snap.json", snapshot())
    secret = _write(tmp_path / "secret.json", {"stolen": TOKEN})
    real_open = os.open

    def racing_open(name, flags, *a, **kw):
        if name == "snap.json":  # attacker swaps the leaf just before open
            target.unlink()
            target.symlink_to(secret)
        return real_open(name, flags, *a, **kw)

    monkeypatch.setattr(obs.os, "open", racing_open)
    with pytest.raises(obs.ObservabilityError):
        obs._read_small_bytes(target, 1 << 20)


def test_parent_replacement_race_is_refused(tmp_path, monkeypatch):
    _write(tmp_path / "state" / "snap.json", snapshot())
    _write(tmp_path / "evil" / "snap.json", {"stolen": TOKEN})
    real_open = os.open

    def racing_open(name, flags, *a, **kw):
        if name == "state":
            os.rename(tmp_path / "state", tmp_path / "state-old")
            os.symlink(tmp_path / "evil", tmp_path / "state")
        return real_open(name, flags, *a, **kw)

    monkeypatch.setattr(obs.os, "open", racing_open)
    with pytest.raises(obs.ObservabilityError):
        obs._read_small_bytes(tmp_path / "state" / "snap.json", 1 << 20)


def test_oversize_file_refused_with_bounded_read(tmp_path):
    p = tmp_path / "big.json"
    p.write_bytes(b" " * 2048)
    with pytest.raises(obs.ObservabilityError):
        obs._read_small_bytes(p, 1024)
    assert len(obs._read_small_bytes(p, 2048)) == 2048


@pytest.mark.parametrize("content", [b"\xff\xfe\x00garbage", b"[" * 200_000, b"{not json", b""])
def test_malicious_content_fails_closed(tmp_path, content):
    p = tmp_path / "snap.json"
    p.write_bytes(content)
    assert obs.load_harness_snapshot(p) == (None, "Harness progress snapshot unreadable or malformed")


def test_fifo_and_directory_refused_without_hanging(tmp_path):
    fifo = tmp_path / "snap.fifo"
    os.mkfifo(fifo)
    with pytest.raises(obs.ObservabilityError):
        obs._read_small_bytes(fifo, 1024)
    with pytest.raises(obs.ObservabilityError):
        obs._read_small_bytes(tmp_path, 1024)


def test_unsupported_platform_fails_closed(tmp_path, monkeypatch):
    p = _write(tmp_path / "snap.json", snapshot())
    monkeypatch.delattr(obs.os, "O_NOFOLLOW")
    with pytest.raises(obs.ObservabilityError):
        obs._read_small_bytes(p, 1 << 20)
    assert obs.load_harness_snapshot(p)[0] is None


def _registry(path, aid=AID):
    return _write(path, {"schema_version": 1, "automations": [{"automation_id": aid,
                                                                "configured_enabled": True}]})


def _links(path, aid=AID):
    return _write(path, {"schema_version": 1, "links": [{"pf_task_id": 7, "automation_id": aid,
                                                         "harness_task_id": 3, "proof_ref": "pr:56",
                                                         "reviewed_on": "2026-11-20"}]})


def test_link_file_and_registry_symlinks_refused(tmp_path):
    reg = _registry(tmp_path / "cfg" / "registry.json")
    links = _links(tmp_path / "cfg" / "links.json")
    assert obs.load_task_links(links, registry_path=reg)[1] == "ok"
    (tmp_path / "l").symlink_to(tmp_path / "cfg", target_is_directory=True)
    assert obs.load_task_links(tmp_path / "l" / "links.json", registry_path=reg) == (
        {}, "link file unreadable or malformed")
    assert obs.load_task_links(links, registry_path=tmp_path / "l" / "registry.json") == (
        {}, "Harness registry unreadable; links refused")


def test_operator_api_with_malicious_snapshot_never_500s(client, tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / "test.db")
    db.execute("INSERT INTO tasks(id,slug,title) VALUES(7,'t7','t7')")
    db.commit()
    db.close()
    # the app's real registry is not under tmp_path; use our own registry/links
    reg = _registry(tmp_path / "cfg" / "registry.json")
    links = _links(tmp_path / "cfg" / "links.json")
    snap = snapshot(gates_passed=TOKEN)
    snap["health"] = "bad"
    p = _write(tmp_path / "state" / "snap.json", snap)
    monkeypatch.setenv("PROJECTFLOW_HARNESS_PROGRESS_JSON", str(p))
    monkeypatch.setenv("PROJECTFLOW_OBS_HARNESS_LINKS", str(links))
    monkeypatch.setenv("PROJECTFLOW_HARNESS_REGISTRY", str(reg))
    r = client.get("/api/tasks/7/observability")
    assert r.status_code == 200
    assert r.json()["completion_percent"] is None and "HARNESS_EVIDENCE_MALFORMED" in r.json()["blockers"]
    html = client.get("/tasks/7/observability")
    assert html.status_code == 200
    assert TOKEN not in r.text and TOKEN not in html.text


# ---------------------------------------------------------------- 5. pre-ledger store upgrade

PRE_LEDGER_DDL = """
CREATE TABLE agent_obs_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE,
  content_hash TEXT NOT NULL, task_id TEXT NOT NULL, session_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
  observed_at TEXT NOT NULL, late INTEGER NOT NULL DEFAULT 0, event_json TEXT NOT NULL);
CREATE INDEX idx_agent_obs_task ON agent_obs_events(task_id, seq);
CREATE TABLE agent_obs_dedupe (dedupe_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, first_seq INTEGER NOT NULL);
CREATE TABLE agent_obs_bindings (session_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, project_id TEXT NOT NULL,
  task_id TEXT NOT NULL, agent_id TEXT NOT NULL, first_event_id TEXT NOT NULL);
CREATE TABLE agent_obs_quarantine (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, reason TEXT NOT NULL,
  detail TEXT NOT NULL, event_id TEXT, source TEXT, flags_json TEXT NOT NULL);
CREATE TABLE agent_obs_counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def raw(eid, kind="tool_call", minutes=1, **kw):
    base = {"schema_version": 1, "event_id": eid, "source": "cli_hook",
            "occurred_at": (NOW - timedelta(minutes=minutes)).isoformat(), "node_id": "nec1",
            "project_id": "projectflow", "task_id": "pf-task-7", "session_id": "s1", "agent_id": "a1",
            "kind": kind, "provenance_level": "LOCAL_OBSERVED"}
    base.update(kw)
    return base


@pytest.fixture
def legacy_db(tmp_path):
    """A store written by the PRE-ledger code: includes the old bug where
    one event_id was stored twice under different dedupe keys."""
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript(PRE_LEDGER_DDL)
    rows = [raw("ok1"), raw("ok2", minutes=2), raw("dup", dedupe_key="k-a"),
            raw("dup", kind="process_exited", dedupe_key="k-b")]
    for r in rows:
        item = obs.normalize(r, observed_at=NOW)
        cur = db.execute("""INSERT INTO agent_obs_events(dedupe_key,content_hash,task_id,session_id,
                            occurred_at,observed_at,late,event_json) VALUES(?,?,?,?,?,?,0,?)""",
                         (item.dedupe_key, item.content_hash(), item.task_id, item.session_id,
                          item.occurred_at, item.observed_at, json.dumps(item.to_public(), sort_keys=True)))
        db.execute("INSERT INTO agent_obs_dedupe VALUES(?,?,?)", (item.dedupe_key, item.content_hash(), cur.lastrowid))
    db.execute("INSERT INTO agent_obs_events(dedupe_key,content_hash,task_id,session_id,occurred_at,"
               "observed_at,event_json) VALUES('junk','h','pf-task-7','s1',?,?,'{not json')",
               (NOW.isoformat(), NOW.isoformat()))
    db.execute("INSERT INTO agent_obs_dedupe VALUES('pruned-key','h',0)")  # event long pruned
    db.commit()
    db.close()
    return path


def test_upgrade_backfills_ledger_and_quarantines_legacy_conflicts(legacy_db):
    store = obs.ObservabilityEventStore(legacy_db)
    ids = sorted(e["event_id"] for e in store.all_task_events("pf-task-7"))
    assert ids == ["ok1", "ok2"]  # both conflicting "dup" rows + junk removed from the timeline
    reasons = [q["reason"] for q in store.quarantine()]
    assert reasons.count("legacy_event_id_conflict") == 2 and reasons.count("legacy_malformed") == 1
    c = store.counters()
    assert c["legacy_event_id_conflicts"] == 2 and c["legacy_malformed"] == 1
    assert c["legacy_pruned_without_event_id"] == 1
    # legacy ids stay bound: no reassignment under a new key, no revival
    assert store.ingest(raw("dup", dedupe_key="k-c"), observed_at=NOW).reason == "event_id_conflict"
    assert store.ingest(raw("ok1", dedupe_key="k-new"), observed_at=NOW).reason == "event_id_conflict"
    assert store.ingest(raw("ok1"), observed_at=NOW).status == "DUPLICATE"
    assert store.ingest(raw("dup", dedupe_key="k-a"), observed_at=NOW).status == "DUPLICATE"
    assert "dup" not in [e["event_id"] for e in store.all_task_events("pf-task-7")]
    assert store.ingest(raw("fresh"), observed_at=NOW).status == "ACCEPTED"


def test_upgrade_is_idempotent_across_restarts(legacy_db):
    obs.ObservabilityEventStore(legacy_db)
    before = obs.ObservabilityEventStore(legacy_db).counters()
    again = obs.ObservabilityEventStore(legacy_db)
    assert again.counters() == before
    db = sqlite3.connect(legacy_db)
    assert db.execute("SELECT COUNT(*) FROM agent_obs_event_ids").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM agent_obs_meta").fetchone()[0] == 1
    db.close()


def test_upgrade_is_atomic_on_failure(legacy_db, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("crash mid-migration")

    monkeypatch.setattr(obs.ObservabilityEventStore, "_quarantine", boom)
    with pytest.raises(RuntimeError):
        obs.ObservabilityEventStore(legacy_db)
    db = sqlite3.connect(legacy_db)
    assert db.execute("SELECT COUNT(*) FROM agent_obs_event_ids").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM agent_obs_events").fetchone()[0] == 5
    assert db.execute("SELECT COUNT(*) FROM agent_obs_meta").fetchone()[0] == 0
    db.close()
    monkeypatch.undo()
    assert sorted(e["event_id"] for e in obs.ObservabilityEventStore(legacy_db).all_task_events("pf-task-7")) == ["ok1", "ok2"]


def test_fresh_store_marks_migration_done_with_no_legacy_counters(tmp_path):
    s = obs.ObservabilityEventStore(tmp_path / "new.db")
    assert not any(k.startswith("legacy_") for k in s.counters())


# ---------------------------------------------------------------- QA last-fix 1: conflicting child clocks

RUNNING = {"id": 3, "state": "RUNNING_VERIFIED", "cleanup": "NOT_STARTED", "time_used_percent": 40,
           "overdue": False, "finished": False}
EXPIRED = {"id": 3, "state": "EXPIRED", "cleanup": "CLEANUP_PENDING", "time_used_percent": 100,
           "overdue": False, "finished": True}


@pytest.mark.parametrize("active,recent", [
    ([RUNNING], [EXPIRED]), ([EXPIRED], [RUNNING]),            # across lists, both orders
    ([RUNNING, EXPIRED], []), ([], [EXPIRED, RUNNING]),        # within one list, both orders
    ([RUNNING, dict(RUNNING)], []),                            # even identical duplicates are ambiguous
])
def test_duplicate_child_id_is_conflict_never_green(active, recent):
    snap = snapshot()
    snap["monitors"][0].update(active_tasks=active, recent_tasks=recent)
    out = attach(snap)
    assert "HARNESS_CHILD_CONFLICT" in out["blockers"]
    assert out["time_budget"]["status"] == "UNKNOWN"
    assert out["execution"] != "RUNNING_VERIFIED"
    assert "child_conflict_overrides_running" in out["reasons"]


def test_identical_copy_in_active_and_recent_is_not_a_conflict():
    # real harness_progress.summarize() lists an open child in both lists
    snap = snapshot()
    snap["monitors"][0].update(active_tasks=[RUNNING], recent_tasks=[dict(RUNNING)])
    out = attach(snap)
    assert out["time_budget"]["status"] == "KNOWN" and "HARNESS_CHILD_CONFLICT" not in out["blockers"]
    assert out["execution"] == "RUNNING_VERIFIED"


def test_duplicate_of_unlinked_child_does_not_block_linked_child():
    snap = snapshot()
    other = dict(RUNNING, id=9)
    snap["monitors"][0]["recent_tasks"] = [other, dict(other, state="EXPIRED")]
    out = attach(snap)
    assert out["time_budget"]["status"] == "KNOWN" and "HARNESS_CHILD_CONFLICT" not in out["blockers"]


# ---------------------------------------------------------------- QA last-fix 3: malformed list elements

@pytest.mark.parametrize("mutate", [
    lambda m: m.append("junk"),
    lambda m: m.append(["list"]),
    lambda m: m.append(None),
    lambda m: m.append({"no_id": True}),
    lambda m: m.append({"id": 5}),
    lambda m: m.append({"id": "has space"}),
    lambda m: m.append({"id": PRIVATE}),
    lambda m: m.insert(0, {"id": AID, "kind": "project", "completion_percent": 100,
                           "gates_passed": 6, "gates_total": 6}),  # duplicate monitor, good-looking
    lambda m: m.append({"id": "OTHER_MON"}) or m.append({"id": "OTHER_MON"}),
])
def test_malformed_or_duplicate_monitor_element_is_unknown(mutate):
    snap = snapshot()
    mutate(snap["monitors"])
    out = attach(snap)
    assert out["completion_percent"] is None
    assert "HARNESS_EVIDENCE_MALFORMED" in out["blockers"]
    assert out["time_budget"]["status"] == "UNKNOWN"
    assert PRIVATE not in json.dumps(out)


@pytest.mark.parametrize("key", ["active_tasks", "recent_tasks"])
@pytest.mark.parametrize("junk", ["EXPIRED", ["EXPIRED"], 3, None, {"state": "EXPIRED"},
                                  {"id": "3", "state": "EXPIRED"}, {"id": True}, {"id": 0}, {"id": -3}])
def test_malformed_child_element_is_unknown_not_partial(key, junk):
    snap = snapshot()
    snap["monitors"][0][key] = list(snap["monitors"][0].get(key, [])) + [junk]
    out = attach(snap)
    assert out["completion_percent"] is None and out["time_budget"]["status"] == "UNKNOWN"
    assert "HARNESS_EVIDENCE_MALFORMED" in out["blockers"]
    assert out["execution"] != "RUNNING_VERIFIED" or out["time_budget"]["status"] == "UNKNOWN"


# ---------------------------------------------------------------- QA last-fix 2: proof_ref privacy

@pytest.mark.parametrize("ref", [
    "ref:/home/alice/private", "pr:/etc/passwd", "issue:/Users/bob/x", "test_runs:1/2",
    "pr:56\\x", "https://example.com/pr/56", "file:///home/alice", "ssh:git@host",
    "pr:" + TOKEN, "token:" + TOKEN, "pr:abc", "pr:0", "pr:-1", "pr:56 ", "PR:56",
    "unknown_type:5", "commit:XYZ", "commit:/home/alice", "pr:56:extra", "",
])
def test_unreviewed_or_path_like_proof_ref_fails_whole_link_file(tmp_path, ref):
    reg = _registry(tmp_path / "cfg" / "registry.json")
    good = {"pf_task_id": 8, "automation_id": AID, "harness_task_id": 3, "proof_ref": "pr:56",
            "reviewed_on": "2026-11-20"}
    bad = dict(good, pf_task_id=7, proof_ref=ref)
    links = _write(tmp_path / "cfg" / "links.json", {"schema_version": 1, "links": [good, bad]})
    out, why = obs.load_task_links(links, registry_path=reg)
    assert out == {} and "proof_ref" in why  # whole file closed, not just the bad entry
    assert ref == "" or ref not in why


@pytest.mark.parametrize("ref", ["pr:56", "issue:12", "test_runs:9", "review_runs:4", "qa_runs:1",
                                 "merge_records:2", "harness_tasks:3", "commit:86b4899", "commit:" + "a" * 40])
def test_allowlisted_proof_refs_accepted(tmp_path, ref):
    reg = _registry(tmp_path / "cfg" / "registry.json")
    links = _write(tmp_path / "cfg" / "links.json", {"schema_version": 1, "links": [
        {"pf_task_id": 7, "automation_id": AID, "harness_task_id": 3, "proof_ref": ref,
         "reviewed_on": "2026-11-20"}]})
    out, why = obs.load_task_links(links, registry_path=reg)
    assert why == "ok" and out[7]["proof_ref"] == ref


def test_path_proof_ref_never_reaches_api_json(client, tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / "test.db")
    db.execute("INSERT INTO tasks(id,slug,title) VALUES(7,'t7','t7')")
    db.commit()
    db.close()
    reg = _registry(tmp_path / "cfg" / "registry.json")
    links = _write(tmp_path / "cfg" / "links.json", {"schema_version": 1, "links": [
        {"pf_task_id": 7, "automation_id": AID, "harness_task_id": 3,
         "proof_ref": "ref:/home/alice/private", "reviewed_on": "2026-11-20"}]})
    monkeypatch.setenv("PROJECTFLOW_HARNESS_PROGRESS_JSON", str(_write(tmp_path / "s" / "snap.json", snapshot())))
    monkeypatch.setenv("PROJECTFLOW_OBS_HARNESS_LINKS", str(links))
    monkeypatch.setenv("PROJECTFLOW_HARNESS_REGISTRY", str(reg))
    r = client.get("/api/tasks/7/observability")
    assert r.status_code == 200
    assert "/home/alice" not in r.text and "/home/alice" not in client.get("/tasks/7/observability").text
    assert r.json()["harness_link"]["automation_id"] is None and r.json()["completion_percent"] is None


# ---------------------------------------------------------------- QA last-fix 4: bounded backfill

def test_backfill_is_bounded_by_retention_cap(legacy_db):
    s = obs.ObservabilityEventStore(legacy_db, max_events=2, max_dedupe=50)
    c = s.counters()
    assert c["stored_events"] <= 2 and c["legacy_pruned_over_retention"] == 3
    db = sqlite3.connect(legacy_db)
    ledgered = db.execute("SELECT COUNT(*) FROM agent_obs_event_ids").fetchone()[0]
    stored = {r[0] for r in db.execute("SELECT dedupe_key FROM agent_obs_events")}
    bound = {r[0] for r in db.execute("SELECT dedupe_key FROM agent_obs_event_ids")}
    db.close()
    assert ledgered <= 2 and stored <= bound  # no surviving row left unledgered
    # exact replay of a pruned historical event is still recognized via the dedupe ledger
    assert s.ingest(raw("ok1"), observed_at=NOW).status == "DUPLICATE"
