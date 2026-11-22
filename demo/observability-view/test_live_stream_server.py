"""Contract tests for public, read-only SSE service and detailed snapshots."""
import json
from pathlib import Path
import http.client
import socket
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_stream_server import Server, detailed_snapshot


def _dbs(tmp_path):
    nodes = tmp_path / "nodes.db"
    sessions = tmp_path / "sessions.db"
    with sqlite3.connect(nodes) as db:
        db.execute("CREATE TABLE nodes (id TEXT,last_known_status TEXT,last_heartbeat_at TEXT,capacity_status TEXT,execution_health_state TEXT,hostname TEXT,auth_token_ref TEXT)")
    with sqlite3.connect(sessions) as db:
        db.execute("CREATE TABLE session_records (node_id TEXT,agent_type TEXT,status TEXT,deleted_at TEXT,last_activity_at TEXT,last_seen_at TEXT,session_name TEXT,launch_command TEXT,last_known_state TEXT,last_checkpoint_at TEXT,last_commit TEXT,git_branch TEXT,stable_session_id TEXT,generation INTEGER DEFAULT 1)")
    return nodes,sessions


def test_snapshot_details_degraded_agents_and_privacy(tmp_path):
    now = datetime(2026,11,21,4,0,tzinfo=timezone.utc)
    recent = (now - timedelta(seconds=15)).isoformat()
    stale = (now - timedelta(minutes=9)).isoformat()
    nodes,sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.executemany("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",[
            ("secret-1","online",recent,"healthy","EXECUTION_OK","private-user-host","ghp_VERY_SECRET"),
            ("secret-2","online",recent,"overloaded","EXECUTION_DOWN","192.168.1.5","token"),
            ("secret-3","offline",stale,"healthy","EXECUTION_DOWN","private","token"),
        ])
    with sqlite3.connect(sessions) as db:
        db.executemany("INSERT INTO session_records(node_id,agent_type,status,deleted_at,last_activity_at,last_seen_at,session_name,launch_command) VALUES(?,?,?,?,?,?,?,?)",[
            ("secret-1","claude","ACTIVE",None,recent,recent,"PRIVATE_CUSTOMER_TASK","claude -p API_TOKEN_SECRET"),
            ("secret-1","codex","ACTIVE",None,stale,stale,"private investigation","codex secret"),
            ("secret-2","shell","ACTIVE",None,recent,recent,"MCP internal private","ssh sensitive"),
            ("secret-3","shell","KILLED",None,stale,stale,"old","secret"),
        ])
    d = detailed_snapshot(nodes,sessions,now=now)
    assert d["nodes"]["total"] == 3
    assert [n["status"] for n in d["nodes"]["items"]] == ["online","degraded","offline"]
    assert d["nodes"]["online"] == 1
    assert d["nodes"]["degraded"] == 1
    assert d["nodes"]["offline"] == 1
    assert d["nodes"]["items"][1]["capacity"] == "overloaded"
    assert d["sessions"]["open"] == 3
    assert d["sessions"]["recent_last_minute"] == 2
    assert d["sessions"]["agent_types"] == {"claude":1,"codex":1,"shell":1,"other":0}
    assert d["acceptance"]["percent"] is None
    assert d["sessions"]["running_verified"] is None
    assert len(d["sessions"]["items"]) == 3
    assert {x["activity_bucket"] for x in d["sessions"]["items"]} == {"under_1m","over_5m"}
    payload=json.dumps(d)
    for forbidden in ("secret-1","PRIVATE_CUSTOMER_TASK","API_TOKEN_SECRET",
                      "private-user-host","192.168.1.5","ghp_VERY_SECRET","MCP internal"):
        assert forbidden not in payload
    assert d["transport"]["poll_seconds"] == 3


def test_session_links_use_only_owner_approved_node_names(tmp_path):
    now = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc)
    nodes, sessions = _dbs(tmp_path)
    ts = now.isoformat()
    with sqlite3.connect(nodes) as db:
        db.executemany("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)", [
            ("hp-linux", "online", ts, "healthy", "EXECUTION_OK", "PRIVATE_HOST", "PRIVATE_TOKEN"),
            ("m910", "online", ts, "healthy", "EXECUTION_OK", "PRIVATE_HOST", "PRIVATE_TOKEN"),
            ("secret-unregistered-node", "offline", ts, "healthy", "EXECUTION_DOWN", "PRIVATE_HOST", "PRIVATE_TOKEN"),
        ])
    with sqlite3.connect(sessions) as db:
        db.executemany("INSERT INTO session_records(node_id,agent_type,status,deleted_at,last_activity_at,last_seen_at,session_name,launch_command) VALUES(?,?,?,?,?,?,?,?)", [
            ("hp-linux", "claude", "ACTIVE", None, ts, ts, "SECRET_SESSION", "SECRET_COMMAND"),
            ("m910", "codex", "ACTIVE", None, ts, ts, "SECRET_SESSION", "SECRET_COMMAND"),
            ("secret-unregistered-node", "shell", "ACTIVE", None, ts, ts, "SECRET_SESSION", "SECRET_COMMAND"),
        ])
    d = detailed_snapshot(nodes, sessions, now=now)
    assert [n["label"] for n in d["nodes"]["items"]] == ["hp-linux", "m910", "Node 03"]
    assert sorted(s["node"] for s in d["sessions"]["items"]) == sorted(["hp-linux", "m910", "Node 03"])
    raw = json.dumps(d)
    assert "secret-unregistered-node" not in raw
    assert "SECRET_SESSION" not in raw and "SECRET_COMMAND" not in raw

def test_get_snapshot_health_and_mutations_are_readonly(tmp_path):
    now=datetime.now(timezone.utc).isoformat()
    nodes,sessions=_dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("ID_SECRET","online",now,"healthy","EXECUTION_OK","PRIVATE_HOST","PRIVATE_TOKEN"))
    server=Server(("127.0.0.1",0),nodes,sessions)
    thr=threading.Thread(target=server.serve_forever,daemon=True)
    thr.start()
    try:
        port=server.server_address[1]
        con=http.client.HTTPConnection("127.0.0.1",port,timeout=5)
        con.request("GET","/projectflow-live/health")
        r=con.getresponse();assert r.status==200; assert json.loads(r.read())["ok"] is True;con.close()
        con=http.client.HTTPConnection("127.0.0.1",port,timeout=5)
        con.request("GET","/projectflow-live/snapshot",headers={"Origin":"https://hungtranbkit.github.io"})
        r=con.getresponse();assert r.status==200
        assert r.getheader("Access-Control-Allow-Origin")=="https://hungtranbkit.github.io"
        assert "no-store" in r.getheader("Cache-Control")
        body=r.read();d=json.loads(body)
        assert d["nodes"]["online"]==1 and d["nodes"]["degraded"]==0
        assert b"PRIVATE" not in body;con.close()
        con=http.client.HTTPConnection("127.0.0.1",port,timeout=5)
        con.request("POST","/projectflow-live/snapshot")
        r=con.getresponse();assert r.status==405;r.read();con.close()
        con=http.client.HTTPConnection("127.0.0.1",port,timeout=5)
        con.request("GET","/private/admin")
        r=con.getresponse();assert r.status==404;r.read();con.close()
        con=http.client.HTTPConnection("127.0.0.1",port,timeout=5)
        con.request("GET","/projectflow-live/events")
        r=con.getresponse();assert r.status==200
        assert "text/event-stream" in r.getheader("Content-Type")
        first=r.readline()
        assert first.startswith(b"retry:")
        line=r.readline()
        assert line.strip()==b""
        line=r.readline()
        assert line.strip()==b"event: status"
        data_line=r.readline()
        assert data_line.startswith(b"data: ")
        payload=json.loads(data_line[6:])
        assert payload["nodes"]["total"]==1
        assert "PRIVATE" not in data_line.decode()
        con.close()
    finally:
        server.shutdown()
        server.server_close()
        thr.join(timeout=3)


def test_named_session_project_task_and_commit_reference_are_not_fabricated(tmp_path):
    now = datetime(2026,11,21,4,0,tzinfo=timezone.utc)
    ts=now.isoformat()
    nodes,sessions=_dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("hp-linux","online",ts,"healthy","EXECUTION_OK","PRIVATE_HOST","PRIVATE_TOKEN"))
    with sqlite3.connect(sessions) as db:
        db.execute("INSERT INTO session_records("
                   "node_id,agent_type,status,deleted_at,last_activity_at,last_seen_at,"
                   "session_name,launch_command,last_known_state,last_checkpoint_at,last_commit,git_branch)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("hp-linux","codex","ACTIVE",None,ts,ts,
                    "zgh-photo-capacity-fix-20261121","secret command",
                    None,None,"abcdef1234567890","feat/photo-fix"))
    d=detailed_snapshot(nodes,sessions,now=now)
    ss=d["sessions"]["items"]
    assert len(ss)==1
    x=ss[0]
    assert x["project"]=="Zalo Group Hub"
    assert x["session"]=="zgh-photo-capacity-fix-20261121"
    assert "ảnh" in x["task_hint"]
    assert x["task_source"]=="session_name_hint"
    assert x["execution_state"]=="OPEN_UNVERIFIED"
    assert x["running_verified"] is None
    assert x["evidence"]=="registry_only"
    assert x["commit_ref"]=="abcdef123456"
    assert x["branch_ref"]=="feat/photo-fix"
    assert d["sessions"]["proven_running"]==0
    raw=json.dumps(d)
    assert "secret command" not in raw and "PRIVATE_HOST" not in raw


def test_unapproved_session_is_not_published_as_raw_title(tmp_path):
    now = datetime(2026,11,21,4,0,tzinfo=timezone.utc)
    ts=now.isoformat()
    nodes,sessions=_dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("m910","online",ts,"healthy","EXECUTION_OK","PRIVATE_HOST","PRIVATE_TOKEN"))
    with sqlite3.connect(sessions) as db:
        db.execute("INSERT INTO session_records("
                   "node_id,agent_type,status,deleted_at,last_activity_at,last_seen_at,"
                   "session_name,launch_command,last_known_state,last_checkpoint_at,last_commit,git_branch)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("m910","claude","ACTIVE",None,ts,ts,
                    "secret-client-private-work","hidden token=secret",
                    None,None,None,None))
    d=detailed_snapshot(nodes,sessions,now=now)
    x=d["sessions"]["items"][0]
    assert x["session"] is None
    assert x["project"]=="Chưa phân loại"
    assert x["execution_state"]=="OPEN_UNVERIFIED"
    assert "secret-client" not in json.dumps(d)


def test_stale_checkpoint_cannot_fake_verified_running(tmp_path):
    now = datetime(2026,11,21,4,0,tzinfo=timezone.utc)
    ts=now.isoformat()
    old=(now-timedelta(hours=2)).isoformat()
    nodes,sessions=_dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("hp-linux","online",ts,"healthy","EXECUTION_OK","PRIVATE_HOST","PRIVATE_TOKEN"))
    with sqlite3.connect(sessions) as db:
        db.executemany("INSERT INTO session_records("
                       "node_id,agent_type,status,deleted_at,last_activity_at,last_seen_at,"
                       "session_name,launch_command,last_known_state,last_checkpoint_at,last_commit,git_branch)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("hp-linux","codex","ACTIVE",None,ts,ts,
             "projectflow-regression-20261121","hidden","RUNNING_VERIFIED",old,None,None),
            ("hp-linux","claude","ACTIVE",None,ts,ts,
             "projectflow-observability-20261121","hidden","RUNNING_VERIFIED",ts,None,None),
        ])
    d=detailed_snapshot(nodes,sessions,now=now)
    assert d["sessions"]["proven_running"]==1
    by_session = {item["session"]: item for item in d["sessions"]["items"]}
    stale = by_session["projectflow-regression-20261121"]
    recent = by_session["projectflow-observability-20261121"]
    assert stale["execution_state"]=="OPEN_UNVERIFIED"
    assert stale["running_verified"] is None
    assert recent["execution_state"]=="RUNNING_VERIFIED"
    assert recent["running_verified"] is True

def test_running_prioritized_across_more_than_24_sessions(tmp_path):
    """The 31st row must be promoted before the public display cap."""
    now = datetime(2026, 11, 21, 9, 40, tzinfo=timezone.utc)
    recent = (now - timedelta(seconds=12)).isoformat()
    stale = (now - timedelta(hours=2)).isoformat()
    nodes, sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("nec1", "online", recent, "healthy", "EXECUTION_OK", "private", "secret"))
    with sqlite3.connect(sessions) as db:
        for i in range(30):
            db.execute("INSERT INTO session_records("
                       "node_id,agent_type,status,deleted_at,last_activity_at,last_seen_at,"
                       "session_name,launch_command,last_known_state,last_checkpoint_at,"
                       "last_commit,git_branch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("nec1", "codex", "ACTIVE", None, recent, recent,
                        "projectflow-observability-%02d" % i, "secret command",
                        "RUNNING_VERIFIED" if i == 0 else None,
                        stale if i == 0 else None, None, None))
        db.execute("INSERT INTO session_records("
                   "node_id,agent_type,status,deleted_at,last_activity_at,last_seen_at,"
                   "session_name,launch_command,last_known_state,last_checkpoint_at,"
                   "last_commit,git_branch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("nec1", "codex", "ACTIVE", None, recent, recent,
                    "zgh-photo-capacity-fix-zzlast", "secret command",
                    "RUNNING_VERIFIED", recent, None, None))
    data = detailed_snapshot(nodes, sessions, now=now)
    assert data["sessions"]["open"] == 31
    assert data["sessions"]["displayed_count"] == 31
    assert data["sessions"]["hidden_count"] == 0
    assert data["sessions"]["proven_running"] == 1
    first = data["sessions"]["items"][0]
    assert first["session"] == "zgh-photo-capacity-fix-zzlast"
    assert first["execution_state"] == "RUNNING_VERIFIED"
    assert first["running_verified"] is True
    assert first["label"] == "Session 01"
    assert data["sessions"]["items"][1]["execution_state"] == "OPEN_UNVERIFIED"
    assert "secret command" not in json.dumps(data)

def test_node_readback_and_curated_title_override_unverified_registry(tmp_path):
    now = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    ts = now.isoformat()
    nodes, sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("hp-linux", "online", ts, "healthy", "EXECUTION_OK", "private", "secret"))
    with sqlite3.connect(sessions) as db:
        db.execute("INSERT INTO session_records("
                   "node_id,agent_type,status,deleted_at,last_activity_at,last_seen_at,"
                   "session_name,launch_command,last_known_state,last_checkpoint_at,"
                   "last_commit,git_branch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("hp-linux", "codex", "ACTIVE", None, ts, ts,
                    "projectflow-observability-20261121", "SECRET_COMMAND", None, None, None, None))
    class Witness:
        def snapshot(self, rows, *, now):
            assert len(rows) == 1
            return {("hp-linux", "projectflow-observability-20261121"): {
                "state": "IDLE", "evidence": "node_observed",
                "observed_at": ts, "task_hint": "Hoàn thiện giám sát hoạt động agent",
                "task_source": "trusted_task_label",
            }}
    d = detailed_snapshot(nodes, sessions, now=now, monitor=Witness())
    s = d["sessions"]["items"][0]
    assert s["execution_state"] == "IDLE"
    assert s["running_verified"] is None
    assert s["task_source"] == "trusted_task_label"
    assert s["task_hint"] == "Hoàn thiện giám sát hoạt động agent"
    assert d["sessions"]["proven_running"] == 0
    assert d["sessions"]["observation_source"] == "terminal_mcp_authenticated_status"
    assert "SECRET_COMMAND" not in json.dumps(d)


def test_live_output_delta_can_verify_running_without_registry_checkpoint(tmp_path):
    now = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    ts = now.isoformat()
    nodes, sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("hp-linux", "online", ts, "healthy", "EXECUTION_OK", "private", "secret"))
    with sqlite3.connect(sessions) as db:
        db.execute("INSERT INTO session_records("
                   "node_id,agent_type,status,deleted_at,last_activity_at,last_seen_at,"
                   "session_name,launch_command,last_known_state,last_checkpoint_at,"
                   "last_commit,git_branch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("hp-linux", "codex", "ACTIVE", None, ts, ts,
                    "projectflow-observability-20261121", "SECRET_COMMAND", None, None, None, None))
    class Witness:
        def snapshot(self, rows, *, now):
            return {("hp-linux", "projectflow-observability-20261121"): {
                "state": "RUNNING_VERIFIED", "evidence": "terminal_output_delta",
                "observed_at": ts, "checkpoint_at": ts,
            }}
    d = detailed_snapshot(nodes, sessions, now=now, monitor=Witness())
    s = d["sessions"]["items"][0]
    assert s["running_verified"] is True
    assert s["last_checkpoint_at"] == ts
    assert s["evidence"] == "terminal_output_delta"
    assert d["sessions"]["proven_running"] == 1
    assert "SECRET_COMMAND" not in json.dumps(d)


def test_live_fleet_replaces_duplicated_stale_registry_and_promotes_remote_running(tmp_path):
    now = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
    recent = now.isoformat()
    nodes, sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        for node in ("hp-linux", "m910", "nec1"):
            db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                       (node, "online", recent, "healthy", "EXECUTION_OK", "private", "secret"))
    with sqlite3.connect(sessions) as db:
        for node, session in (
            ("local", "ota-qa-hp-20261121"),
            ("hp-linux", "ota-qa-hp-20261121"),   # duplicate legacy alias
            ("hp-linux", "old-stale-20261101"),  # not actually open
            ("m910", "projectflow-observability-remote"),
        ):
            db.execute("INSERT INTO session_records (node_id,agent_type,status,deleted_at,"
                       "last_activity_at,last_seen_at,session_name,launch_command)"
                       " VALUES(?,?,?,?,?,?,?,?)",
                       (node, "shell", "ACTIVE", None, recent, recent, session,
                        "PRIVATE_COMMAND_TOKEN"))
    class FleetReader:
        def list_sessions(self):
            return {"covered_nodes": ["hp-linux", "m910", "nec1"],
                    "unreachable_nodes": [],
                    "sessions": [
                        {"node_id": "hp-linux", "session_name": "ota-qa-hp-20261121", "instance_hint": "11"},
                        {"node_id": "m910", "session_name": "projectflow-observability-remote", "instance_hint": "12"},
                        {"node_id": "nec1", "session_name": "zgh-autoreply-nec-20261122", "instance_hint": "13"},
                    ]}
    class Witness:
        reader = FleetReader()
        def snapshot(self, rows, *, now):
            assert len(rows) == 3
            return {("hp-linux", "ota-qa-hp-20261121"): {
                        "state": "IDLE", "evidence": "node_observed",
                        "observed_at": recent, "agent_type": "shell"},
                    ("m910", "projectflow-observability-remote"): {
                        "state": "RUNNING_VERIFIED", "evidence": "terminal_output_delta",
                        "observed_at": recent, "checkpoint_at": recent, "agent_type": "codex"},
                    ("nec1", "zgh-autoreply-nec-20261122"): {
                        "state": "WAITING_INPUT", "evidence": "node_observed",
                        "observed_at": recent, "agent_type": "claude"}}
    d = detailed_snapshot(nodes, sessions, now=now, monitor=Witness())
    s = d["sessions"]
    assert s["open"] == 3 and s["displayed_count"] == 3
    assert s["hidden_count"] == 0 and s["proven_running"] == 1
    assert s["recent_activity_signals"] == 1 and s["recent_last_minute"] == 1
    assert [x["execution_state"] for x in s["items"]] == [
        "RUNNING_VERIFIED", "WAITING_INPUT", "IDLE"]
    assert s["items"][0]["node"] == "m910"
    assert s["items"][0]["session"] is None  # remotely discovered raw identity is private
    assert s["items"][0]["agent_type"] == "codex"
    assert s["observation_source"] == "terminal_mcp_authenticated_fleet"
    assert {i["label"]: i["open_sessions"] for i in d["nodes"]["items"]} == {
        "hp-linux": 1, "m910": 1, "nec1": 1}
    assert s["coverage"]["covered_nodes"] == ["hp-linux", "m910", "nec1"]
    assert "old-stale" not in json.dumps(d)
    assert "projectflow-observability-remote" not in json.dumps(d)
    assert "PRIVATE_COMMAND_TOKEN" not in json.dumps(d)


def test_incomplete_fleet_coverage_does_not_claim_old_remote_session_open(tmp_path):
    now = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
    ts = now.isoformat()
    nodes, sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        for node in ("hp-linux", "m910"):
            db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                       (node, "online", ts, "healthy", "EXECUTION_OK", "private", "secret"))
    with sqlite3.connect(sessions) as db:
        for node, name in (("local", "ota-qa-hp-20261121"),
                           ("m910", "projectflow-observability-old")):
            db.execute("INSERT INTO session_records (node_id,agent_type,status,deleted_at,"
                       "last_activity_at,last_seen_at,session_name,launch_command)"
                       " VALUES(?,?,?,?,?,?,?,?)",
                       (node, "codex", "ACTIVE", None, ts, ts, name, "SECRET"))
    class Reader:
        def list_sessions(self):
            return {"covered_nodes": ["hp-linux"], "unreachable_nodes": ["m910"],
                    "sessions": [{"node_id": "hp-linux", "session_name": "ota-qa-hp-20261121"}]}
    class Monitor:
        reader = Reader()
        def snapshot(self, rows, *, now):
            return {("hp-linux", "ota-qa-hp-20261121"):
                    {"state":"IDLE","evidence":"node_observed","observed_at":ts}}
    d = detailed_snapshot(nodes, sessions, now=now, monitor=Monitor())
    assert d["sessions"]["open"] == 1
    assert d["sessions"]["proven_running"] == 0
    assert d["sessions"]["coverage"]["unreachable_nodes"] == ["m910"]
    assert "projectflow-observability-old" not in json.dumps(d)
    assert d["sessions"]["recent_activity_signals"] == 0  # heartbeat is not progress


def test_fleet_source_unavailable_does_not_count_stale_active_rows(tmp_path):
    now = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
    ts = now.isoformat()
    nodes, sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("hp-linux", "online", ts, "healthy", "EXECUTION_OK", "host", "ref"))
    with sqlite3.connect(sessions) as db:
        db.execute("INSERT INTO session_records (node_id,agent_type,status,deleted_at,"
                   "last_activity_at,last_seen_at,session_name,launch_command)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   ("local", "codex", "ACTIVE", None, ts, ts,
                    "projectflow-observability-stale", "old command"))
    class UnreachableReader:
        def list_sessions(self):
            return {"covered_nodes": [], "unreachable_nodes": ["hp-linux"],
                    "sessions": []}
    class Monitor:
        reader = UnreachableReader()
        def snapshot(self, rows, *, now):
            assert not rows
            return {}
    data = detailed_snapshot(nodes, sessions, now=now, monitor=Monitor())
    assert data["sessions"]["open"] == 0
    assert data["sessions"]["coverage"]["unreachable_nodes"] == ["hp-linux"]
    assert data["sessions"]["proven_running"] == 0
    assert "projectflow-observability-stale" not in json.dumps(data)


def test_known_assignment_does_not_promote_unverified_execution(tmp_path):
    now = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)
    ts = now.isoformat()
    nodes, sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("m910", "online", ts, "healthy", "EXECUTION_OK", "host", "ref"))
    with sqlite3.connect(sessions) as db:
        db.execute("INSERT INTO session_records (node_id,agent_type,status,deleted_at,"
                   "last_activity_at,last_seen_at,session_name,launch_command)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   ("m910", "codex", "ACTIVE", None, ts, ts, "projectflow-remote",
                    "internal command"))
    class Witness:
        def snapshot(self, rows, *, now):
            return {("m910", "projectflow-remote"): {
                "state": "OPEN_UNVERIFIED", "evidence": "registry_only",
                "observed_at": ts, "task_hint": "Hoàn thiện giám sát hoạt động agent",
                "task_source": "trusted_task_label"}}
    d = detailed_snapshot(nodes, sessions, now=now, monitor=Witness())
    row = d["sessions"]["items"][0]
    assert row["task_hint"] == "Hoàn thiện giám sát hoạt động agent"
    assert row["task_source"] == "trusted_task_label"
    assert row["execution_state"] == "OPEN_UNVERIFIED"
    assert row["running_verified"] is None
    assert d["sessions"]["proven_running"] == 0
    assert "internal command" not in json.dumps(d)


def test_public_running_activity_hints_are_allowlisted_and_removed_on_idle(tmp_path):
    now = datetime(2026, 11, 22, 8, tzinfo=timezone.utc)
    ts = now.isoformat()
    nodes, sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("hp-linux", "online", ts, "healthy", "EXECUTION_OK", "private", "secret"))
    with sqlite3.connect(sessions) as db:
        for name in ("projectflow-running-proof", "projectflow-idle-proof"):
            db.execute("INSERT INTO session_records (node_id,agent_type,status,deleted_at,"
                       "last_activity_at,last_seen_at,session_name,launch_command)"
                       " VALUES(?,?,?,?,?,?,?,?)",
                       ("hp-linux", "codex", "ACTIVE", None, ts, ts, name,
                        "PRIVATE_COMMAND_TOKEN"))
    class Witness:
        def snapshot(self, rows, *, now):
            return {
                ("hp-linux", "projectflow-running-proof"): {
                    "state": "RUNNING_VERIFIED", "evidence": "agent_ui_busy",
                    "observed_at": ts, "checkpoint_at": ts,
                    "recent_actions": [
                        "Có dòng báo đang chạy kiểm thử",
                        "SECRET_PRIVATE_PASSWORD",
                        "Có dòng báo build hoặc biên dịch",
                    ]},
                ("hp-linux", "projectflow-idle-proof"): {
                    "state": "IDLE", "evidence": "node_observed",
                    "observed_at": ts, "recent_actions": [
                        "Có dòng báo đang chạy kiểm thử"
                    ]},
            }
    data = detailed_snapshot(nodes, sessions, now=now, monitor=Witness())
    # Public session names may be masked; classify by verified state.
    run = next(x for x in data["sessions"]["items"]
               if x["execution_state"] == "RUNNING_VERIFIED")
    idle = next(x for x in data["sessions"]["items"]
                if x["execution_state"] == "IDLE")
    assert run["recent_actions"] == [
        "Có dòng báo đang chạy kiểm thử", "Có dòng báo build hoặc biên dịch"
    ]
    assert idle["recent_actions"] == []
    assert data["sessions"]["proven_running"] == 1
    assert "SECRET_PRIVATE_PASSWORD" not in json.dumps(data)
    assert "PRIVATE_COMMAND_TOKEN" not in json.dumps(data)


def test_live_snapshot_adds_sanitized_dell_ai_usage_without_leaking_account(tmp_path):
    now = datetime(2026, 11, 22, 3, 30, tzinfo=timezone.utc)
    ts = now.isoformat()
    nodes, sessions = _dbs(tmp_path)
    with sqlite3.connect(nodes) as db:
        db.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?)",
                   ("dell-linux", "online", ts, "healthy", "EXECUTION_OK", "private", "secret"))
    class Reader:
        def usage_summary(self):
            return {"schema_version": 1, "kind": "terminal-mcp-public-ai-usage",
                    "source": "dell-local-ai-usage-monitor", "source_status": "ok",
                    "sampled_at": ts,
                    "account": "private@example.invalid", "password": "not-for-public",
                    "providers": [
                        {"provider": "codex", "state": "available",
                         "windows": {"weekly": {"used_percent": 13.0,
                                                "remaining_percent": 87.0,
                                                "reset_at": None}}},
                        {"provider": "claude", "state": "not_observed", "windows": {}},
                        {"provider": "gemini", "state": "not_observed", "windows": {}},
                        {"provider": "antigravity", "state": "not_observed", "windows": {}},
                    ]}
    class Monitor:
        reader = Reader()
        def snapshot(self, rows, *, now):
            return {}
    d = detailed_snapshot(nodes, sessions, now=now, monitor=Monitor())
    assert d["usage"]["status"] == "ok"
    assert d["usage"]["providers"][0]["windows"]["weekly"]["used_percent"] == 13.0
    assert d["sessions"]["proven_running"] == 0  # quota never implies a running task
    assert "private@example.invalid" not in json.dumps(d)
    assert "not-for-public" not in json.dumps(d)


def test_usage_source_failure_does_not_take_down_session_overview(tmp_path):
    now = datetime(2026, 11, 22, 3, 30, tzinfo=timezone.utc)
    nodes, sessions = _dbs(tmp_path)
    class Broken:
        def usage_summary(self):
            raise PermissionError("private account must not be printed")
    class Monitor:
        reader = Broken()
        def snapshot(self, rows, *, now):
            return {}
    d = detailed_snapshot(nodes, sessions, now=now, monitor=Monitor())
    assert d["usage"]["status"] == "unavailable"
    assert all(p["windows"] == {} for p in d["usage"]["providers"])
    assert "PermissionError" not in json.dumps(d)
