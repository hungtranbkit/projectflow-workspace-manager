"""Privacy/accuracy regression for public aggregate live view snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_live_snapshot import snapshot


def _make_dbs(tmp_path):
    nodes = tmp_path / "nodes.db"
    sessions = tmp_path / "sessions.db"
    with sqlite3.connect(nodes) as db:
        db.execute("CREATE TABLE nodes (id TEXT, last_known_status TEXT, last_heartbeat_at TEXT, hostname TEXT, auth_token_ref TEXT)")
    with sqlite3.connect(sessions) as db:
        db.execute("CREATE TABLE session_records (node_id TEXT, status TEXT, deleted_at TEXT, last_seen_at TEXT, last_activity_at TEXT, session_name TEXT, launch_command TEXT, notes TEXT)")
    return nodes, sessions


def _insert(dbpath, statement, rows):
    with sqlite3.connect(dbpath) as db:
        db.executemany(statement, rows)


def test_snapshot_live_counts_are_correct_and_no_secret_leaks(tmp_path):
    now = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(seconds=40)).isoformat()
    old = (now - timedelta(hours=2)).isoformat()
    nodes, sessions = _make_dbs(tmp_path)
    _insert(nodes, "INSERT INTO nodes VALUES (?,?,?,?,?)", [
        ("secret-node-alpha", "online", fresh, "private-lan.example", "ghp_PRIVATE_TEST_TOKEN"),
        ("secret-node-beta", "online", old, "192.168.1.5", "dummy-secret"),
        ("secret-node-gamma", "offline", old, "username@example.org", "dummy-secret"),
    ])
    _insert(sessions, "INSERT INTO session_records VALUES (?,?,?,?,?,?,?,?)", [
        ("secret-node-alpha", "ACTIVE", None, fresh, fresh,
         "Confidential Task Patient-123", "claude -p TOP_SECRET", "private path /home/user"),
        ("secret-node-alpha", "ACTIVE", None, fresh, old,
         "Sensitive R&D", "token=SECRET", "private notes"),
        ("secret-node-beta", "ACTIVE", None, old, old,
         "Other task", "other launch", "do not leak"),
        ("secret-node-gamma", "KILLED", None, old, old,
         "stopped", "command", "note"),
    ])
    got = snapshot(nodes, sessions, now=now)
    assert got["nodes"]["total"] == 3
    assert [x["status"] for x in got["nodes"]["items"]] == ["online", "unknown", "offline"]
    assert got["nodes"]["online"] == 1
    assert got["sessions"]["open"] == 3
    assert got["sessions"]["recent_activity_signals"] == 1
    assert got["sessions"]["running_verified"] is None
    assert got["acceptance"]["percent"] is None
    assert got["acceptance"]["ttl_percent"] is None
    assert got["nodes"]["items"][0]["open_sessions"] == 2
    assert got["nodes"]["items"][0]["recent_activity_signals"] == 1
    wire = json.dumps(got, ensure_ascii=False)
    for forbidden in ("secret-node-alpha", "secret-node-beta", "Confidential Task",
                      "192.168.", "ghp_PRIVATE_TEST_TOKEN", "TOP_SECRET",
                      "/home/user", "username@", "private notes"):
        assert forbidden not in wire
    assert "Node 01" in wire


def test_owner_approved_node_names_visible_but_unknown_ids_remain_private(tmp_path):
    now = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)
    n, s = _make_dbs(tmp_path)
    _insert(n, "INSERT INTO nodes VALUES (?,?,?,?,?)", [
        ("hp-linux", "online", now.isoformat(), "private-hostname", "PRIVATE_TOKEN"),
        ("m910", "online", now.isoformat(), "hidden", "ANOTHER_TOKEN"),
        ("private-machine-should-not-leak", "offline", now.isoformat(), "sensitive", "PRIVATE_TOKEN"),
    ])
    data = snapshot(n, s, now=now)
    assert [x["label"] for x in data["nodes"]["items"]] == [
        "hp-linux", "m910", "Node 03"
    ]
    raw = json.dumps(data, ensure_ascii=False)
    assert "private-machine-should-not-leak" not in raw
    assert "private-hostname" not in raw and "PRIVATE_TOKEN" not in raw

def test_stale_online_node_is_not_reported_online(tmp_path):
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    n, s = _make_dbs(tmp_path)
    _insert(n, "INSERT INTO nodes VALUES (?,?,?,?,?)", [
        ("a", "online", (now - timedelta(days=1)).isoformat(), "host", "secret"),
    ])
    data = snapshot(n, s, now=now)
    assert data["nodes"]["unknown"] == 1
    assert data["nodes"]["online"] == 0


def test_bad_source_fails_instead_of_claiming_pass(tmp_path):
    n, s = _make_dbs(tmp_path)
    with sqlite3.connect(n) as db:
        db.execute("DROP TABLE nodes")
    try:
        snapshot(n, s)
    except sqlite3.Error:
        pass
    else:
        raise AssertionError("malformed real source was not rejected")


def test_fresh_window_and_naive_times_bounded(tmp_path):
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    n, s = _make_dbs(tmp_path)
    _insert(n, "INSERT INTO nodes VALUES (?,?,?,?,?)", [
        ("a", "online", now.replace(tzinfo=None).isoformat(), "host", "secret")
    ])
    assert snapshot(n, s, now=now)["nodes"]["online"] == 1
    for seconds in (0, 59, 3601, 99999):
        try:
            snapshot(n, s, now=now, fresh_seconds=seconds)
        except ValueError:
            pass
        else:
            raise AssertionError("unbounded freshness accepted")
