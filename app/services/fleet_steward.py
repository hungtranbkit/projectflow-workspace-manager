"""Private operator memory. A capture is atomic, immutable and scoped to one node."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

NODES = ("dell-linux", "hp-linux", "m910", "macbook", "nec1")
SOURCES = {"git", "systemd-user", "systemd-system", "docker", "android", "phone-storage", "control-path"}
COVERAGE = {"ok", "partial", "unavailable", "not-configured"}
FIELDS = ("source", "name", "project", "state", "version", "location")
VN = timezone(timedelta(hours=7))
SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
 node TEXT NOT NULL, at INTEGER NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(node,at)
);
CREATE TABLE IF NOT EXISTS payloads (digest TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS captures_time ON captures(at);
CREATE TABLE IF NOT EXISTS tickets (digest TEXT PRIMARY KEY, expires INTEGER NOT NULL, kind TEXT NOT NULL);
"""


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def validate(report):
    if set(report) != {"node", "at", "sources", "items"} or report["node"] not in NODES:
        raise ValueError("Invalid report envelope")
    if type(report["at"]) is not int or not 0 < report["at"] <= time.time() + 120:
        raise ValueError("Invalid observation time")
    src = report["sources"]
    if not isinstance(src, dict) or set(src) != SOURCES or any(v not in COVERAGE for v in src.values()):
        raise ValueError("Invalid source coverage")
    if not isinstance(report["items"], list) or len(report["items"]) > 8000:
        raise ValueError("Invalid inventory size")
    identities = set()
    for row in report["items"]:
        if set(row) != set(FIELDS) or row["source"] not in SOURCES or not row["name"]:
            raise ValueError("Invalid evidence")
        if any(not isinstance(row[k], str) or len(row[k]) > 440 or any(ord(c) < 32 for c in row[k]) for k in FIELDS):
            raise ValueError("Invalid evidence value")
        key = (row["source"], row["name"])
        if key in identities:
            raise ValueError("Duplicate identity")
        identities.add(key)


class Memory:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript(SCHEMA)
        self.path.chmod(0o600)

    def ingest(self, report):
        validate(report)
        body = json.dumps({"sources": report["sources"], "items": sorted(report["items"], key=lambda r: (r["source"], r["name"]))}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        fingerprint = digest(body)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT digest FROM captures WHERE node=? AND at=?", (report["node"], report["at"])).fetchone()
            if old and old[0] != fingerprint:
                raise ValueError("Conflicting replay")
            db.execute("INSERT OR IGNORE INTO payloads VALUES (?,?)", (fingerprint, body))
            db.execute("INSERT OR IGNORE INTO captures VALUES (?,?,?)", (report["node"], report["at"], fingerprint))
        return not old

    def issue(self, minutes=15):
        token = secrets.token_urlsafe(32)
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM tickets WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO tickets VALUES (?,?,?)", (digest(token), int(time.time()+minutes*60), "invite"))
        return token

    def redeem(self, token):
        if not isinstance(token, str) or len(token) != 43:
            return None
        session = secrets.token_urlsafe(32)
        with closing(self.connect()) as db, db:
            ok = db.execute("DELETE FROM tickets WHERE digest=? AND expires>=? AND kind='invite'", (digest(token),time.time())).rowcount
            if not ok:
                return None
            db.execute("INSERT INTO tickets VALUES (?,?,?)", (digest(session), int(time.time()+21*86400), "session"))
        return session

    def authorized(self, token):
        if not token or len(token) != 43 or not self.path.is_file():
            return False
        with closing(self.connect()) as db:
            return bool(db.execute("SELECT 1 FROM tickets WHERE digest=? AND expires>=? AND kind='session'", (digest(token),time.time())).fetchone())

    def revoke(self, token):
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM tickets WHERE digest=? AND kind='session'", (digest(token or ""),))

    def report(self, node, until):
        with closing(self.connect()) as db:
            hit = db.execute("SELECT c.at,p.body FROM captures c JOIN payloads p USING(digest) WHERE c.node=? AND c.at<=? ORDER BY c.at DESC LIMIT 1", (node,until)).fetchone()
        return (hit["at"],json.loads(hit["body"])) if hit else (None,{})

    def changes(self, node, start, end, match, limit=80):
        with closing(self.connect()) as db:
            prior = db.execute("SELECT at,digest FROM captures WHERE node=? AND at<? ORDER BY at DESC LIMIT 1",(node,start)).fetchone()
            rows = db.execute("""SELECT at,digest FROM captures WHERE node=? AND at BETWEEN ? AND ? ORDER BY at""",(node,start,end))
            previous = {}
            prev_hash = None
            out = []
            for sample in ([prior] if prior else []):
                payload=json.loads(db.execute("SELECT body FROM payloads WHERE digest=?",(sample["digest"],)).fetchone()[0])
                previous={(r["source"],r["name"]):r for r in payload["items"] if match(r)}
                prev_hash=sample["digest"]
            for sample in rows:
                if sample["digest"] == prev_hash:
                    continue
                body=json.loads(db.execute("SELECT body FROM payloads WHERE digest=?",(sample["digest"],)).fetchone()[0])
                latest={(r["source"],r["name"]):r for r in body["items"] if match(r)}
                for key in sorted(previous.keys() | latest.keys()):
                    old,new=previous.get(key),latest.get(key)
                    if new == old or (not new and body["sources"][key[0]] != "ok"):
                        continue
                    verb="Lần đầu thấy" if not old else "Hết xuất hiện" if not new else "Thay đổi"
                    out.append(dict(at=sample["at"],node=node,event=verb,before=old,after=new))
                # A partial/disconnected source preserves last known evidence for the next comparison.
                previous={**{k:v for k,v in previous.items() if body["sources"][k[0]]!="ok"},**latest}
                prev_hash=sample["digest"]
                out=out[-limit:]
        return out


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFD", text.lower().replace("đ","d")) if not unicodedata.combining(c))


def read_view(memory, q="", node="", project="", when="", intent=""):
    now=int(time.time())
    words=fold(q)
    if not node:
        node=next((n for n in NODES if n in words),"")
    if not project and ("zalo" in words or "s7" in words or "iphone" in words):
        project="zalo-group-hub"
    if node and node not in NODES:
        raise ValueError("Node không hợp lệ")
    if not when:
        date=re.search(r"\b\d{4}-\d\d-\d\d\b",words)
        if date: when=date.group()
        elif "hom qua" in words: when=(datetime.now(VN).date()-timedelta(days=1)).isoformat()
    today=datetime.now(VN).date()
    day=datetime.strptime(when,"%Y-%m-%d").date() if when else today
    if day>today: raise ValueError("Chưa có dữ liệu cho ngày tương lai")
    start=int(datetime.combine(day,datetime.min.time(),VN).timestamp())
    end=min(now,start+86399)
    at_date=bool(when)
    mode=intent or ("history" if at_date or any(w in words for w in ("lich su","qua khu","thay doi","vua qua")) else "next" if any(w in words for w in ("tiep","can lam","tuong lai","uu tien","con thieu")) else "now")
    if mode not in ("now","history","next"): raise ValueError("Loại câu hỏi không hợp lệ")
    nodes=(node,) if node else NODES
    observed={n:memory.report(n,end) for n in nodes}
    projects=sorted({r["project"] for _,body in observed.values() for r in body.get("items",[]) if r["project"]})
    if not project:
        project=next((p for p in sorted(projects,key=len,reverse=True) if p.lower() in words or p.replace("-"," ") in words),"")
    match=lambda r: not project or r["project"]==project
    summaries=[]; items=[]; events=[]; actions=[]
    for n,(at,body) in observed.items():
        fresh=bool(at and end-at<=900)
        rows=[r for r in body.get("items",[]) if match(r)]
        summaries.append(dict(node=n,at=at,fresh=fresh,count=len(rows),sources=body.get("sources",{})))
        if not fresh:
            actions.append(dict(node=n,reason="Thu lại bản đo từ node.",source="Nhịp thu thập",at=at))
        for r in rows:
            items.append(dict(**r,node=n,at=at,fresh=fresh))
            if r["source"] in ("systemd-user","docker") and not r["project"]:
                actions.append(dict(node=n,reason="Xác định dự án của "+r["name"],source=r["source"],at=at))
        for s,status in body.get("sources",{}).items():
            if status in ("partial","unavailable"):
                actions.append(dict(node=n,reason="Xác minh phạm vi thu thập "+s+" ("+status+")",source=s,at=at))
        if mode=="history":
            events.extend(memory.changes(n,start if at_date else 0,end,match))
    if project=="zalo-group-hub" and not any(r["source"]=="phone-storage" for r in items):
        actions.insert(0,dict(node="m910",reason="Xác minh thư mục catalog và ảnh trên điện thoại mục tiêu.",source="phone-storage",at=None))
    if project=="zalo-group-hub" and not any(r["source"]=="control-path" for r in items):
        actions.insert(0,dict(node="m910",reason="Xác minh đường điều khiển iPhone và phụ thuộc M910.",source="control-path",at=None))
    return dict(mode=mode,project=project,node=node,when=when,projects=projects,nodes=summaries,items=items,events=sorted(events,key=lambda e:e["at"],reverse=True)[:100],actions=actions,at=end)
