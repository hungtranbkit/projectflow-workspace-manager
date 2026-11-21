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
        db.execute("CREATE TABLE session_records (node_id TEXT,agent_type TEXT,status TEXT,deleted_at TEXT,last_activity_at TEXT,last_seen_at TEXT,session_name TEXT,launch_command TEXT)")
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
        db.executemany("INSERT INTO session_records VALUES(?,?,?,?,?,?,?,?)",[
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
