"""Private Architecture Center (#91): immutable architecture snapshots per project.

A snapshot pins ONE source commit of one canonical project and holds the native
HTML output of each upstream tool (Archify, Floorplan) rendered from that same
commit, plus a manifest with tool versions, hashes, sizes, scanner coverage,
validation receipts, limitations and provenance. Rows are content addressed and
never updated: a re-import of identical content is a no-op, anything else is a
new snapshot. Artifacts live in the private steward store (no static/public
path) and are served only through the authenticated, sandboxed route.

Generated HTML is untrusted: it is never inlined into a ProjectFlow page and is
only rendered in an opaque-origin sandbox with network access denied.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import zlib
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = "pf-architecture-snapshot/1"
KINDS = ("archify", "floorplan")
ORIGINS = ("imported-pilot", "rerun")
SOURCE_TYPES = ("git-commit",)
STATUSES = ("PASS", "PARTIAL", "FAIL", "NOT_RUN")
MAX_HTML = 8 * 1024 * 1024
MAX_AUX = 4 * 1024 * 1024
MAX_TEXT = 600
MAX_LIST = 40
KEEP_SNAPSHOTS = 40
FRESH_SECONDS = 7 * 86400

PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/@ -]{0,199}$")

# Credential shapes refused in any stored artifact or manifest.
SECRET_PATTERNS = tuple(
    re.compile(p)
    for p in (
        rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
        rb"\bghp_[A-Za-z0-9]{36}\b",
        rb"\bgho_[A-Za-z0-9]{36}\b",
        rb"\bgithub_pat_[A-Za-z0-9_]{60,}",
        rb"\bAKIA[0-9A-Z]{16}\b",
        rb"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        rb"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}",
        rb"\bAIza[0-9A-Za-z_-]{35}\b",
    )
)
# An artifact must be self-contained: no element may load a remote resource.
REMOTE_LOAD = re.compile(
    rb"<(?:script|link|iframe|frame|embed|object|img|source|video|audio)\b[^>]*?\b(?:src|href|data)\s*=\s*[\"']?\s*(?:https?:)?//",
    re.I,
)
DOCTYPE = re.compile(rb"^(?:\xef\xbb\xbf)?\s*<!doctype html", re.I)
HOME_PATH = re.compile(rb"/home/[a-z_][a-z0-9_-]*/")

SCHEMA = """
CREATE TABLE IF NOT EXISTS arch_blobs (
 sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, body BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS arch_snapshots (
 id TEXT PRIMARY KEY, project TEXT NOT NULL, commit_sha TEXT NOT NULL,
 generated_at INTEGER NOT NULL, imported_at INTEGER NOT NULL,
 origin TEXT NOT NULL, manifest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS arch_snapshots_project ON arch_snapshots(project, generated_at);
"""


class ArtifactRejected(ValueError):
    """Input refused by validation; the message is safe to show an operator."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _text(value, field, *, empty=False, limit=MAX_TEXT):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise ArtifactRejected(f"{field}: invalid text")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ArtifactRejected(f"{field}: control characters")
    if any(p.search(value.encode()) for p in SECRET_PATTERNS):
        raise ArtifactRejected(f"{field}: credential-shaped value")
    return value


def _texts(value, field):
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise ArtifactRejected(f"{field}: invalid list")
    return [_text(v, field) for v in value]


def _int(value, field, lo=0, hi=10**12):
    if type(value) is not int or not lo <= value <= hi:
        raise ArtifactRejected(f"{field}: invalid number")
    return value


def _keys(obj, field, required, optional=()):
    if not isinstance(obj, dict):
        raise ArtifactRejected(f"{field}: object required")
    missing = set(required) - set(obj)
    extra = set(obj) - set(required) - set(optional)
    if missing or extra:
        raise ArtifactRejected(f"{field}: fields {sorted(missing)} missing, {sorted(extra)} not allowed")


def validate_project(project: str) -> str:
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project) or ".." in project:
        raise ArtifactRejected("project: canonical owner/repo ID required")
    return project


def validate_graph(graph, field):
    _keys(graph, field, ("nodes", "edges"))
    if not isinstance(graph["nodes"], list) or len(graph["nodes"]) > 5000:
        raise ArtifactRejected(f"{field}.nodes: invalid")
    if not isinstance(graph["edges"], list) or len(graph["edges"]) > 20000:
        raise ArtifactRejected(f"{field}.edges: invalid")
    ids = set()
    for n in graph["nodes"]:
        if not (isinstance(n, list) and len(n) == 2):
            raise ArtifactRejected(f"{field}.nodes: [id,label] pairs required")
        ids.add(_text(n[0], field + ".node", limit=200))
        _text(n[1], field + ".label", empty=True, limit=200)
    for e in graph["edges"]:
        if not (isinstance(e, list) and len(e) == 3):
            raise ArtifactRejected(f"{field}.edges: [from,to,kind] required")
        if e[0] not in ids or e[1] not in ids:
            raise ArtifactRejected(f"{field}.edges: unknown node")
        _text(e[2], field + ".kind", empty=True, limit=200)


def validate_manifest(m: dict) -> dict:
    """Strict, closed schema. Returns the manifest unchanged when valid."""
    _keys(m, "manifest", ("schema", "project", "source", "generated_at", "origin", "provenance", "tools", "artifacts"),
          ("limitations",))
    if m["schema"] != SCHEMA_VERSION:
        raise ArtifactRejected("schema: unsupported version")
    validate_project(m["project"])
    src = m["source"]
    _keys(src, "source", ("type", "commit", "committed_at"), ("branch", "input_manifest_sha256", "file_count"))
    if src["type"] not in SOURCE_TYPES or not SHA1_RE.fullmatch(src["commit"] or ""):
        raise ArtifactRejected("source: full 40-hex commit required")
    _text(src["committed_at"], "source.committed_at", limit=40)
    if "branch" in src:
        _text(src["branch"], "source.branch", limit=200)
    if "input_manifest_sha256" in src and not SHA256_RE.fullmatch(src["input_manifest_sha256"]):
        raise ArtifactRejected("source.input_manifest_sha256: invalid")
    if "file_count" in src:
        _int(src["file_count"], "source.file_count", 0, 100000)
    _int(m["generated_at"], "generated_at", 1_600_000_000, int(time.time()) + 300)
    if m["origin"] not in ORIGINS:
        raise ArtifactRejected("origin: invalid")
    prov = m["provenance"]
    _keys(prov, "provenance", ("summary",), ("operator", "run_task", "evidence_refs", "export_ref"))
    _text(prov["summary"], "provenance.summary")
    for k in ("operator", "export_ref"):
        if k in prov:
            _text(prov[k], "provenance." + k, limit=200)
    if "run_task" in prov:
        _int(prov["run_task"], "provenance.run_task", 1)
    if "evidence_refs" in prov:
        _texts(prov["evidence_refs"], "provenance.evidence_refs")
    tools = m["tools"]
    if not isinstance(tools, dict) or not set(tools) <= set(KINDS) | {"runtime"} or not set(tools) & set(KINDS):
        raise ArtifactRejected("tools: archify/floorplan/runtime entries only")
    for name, t in tools.items():
        _keys(t, "tools." + name, ("version",), ("commit", "license", "source", "image", "commands"))
        _text(t["version"], f"tools.{name}.version", limit=120)
        if "commit" in t and not SHA1_RE.fullmatch(t["commit"]):
            raise ArtifactRejected(f"tools.{name}.commit: full SHA required")
        for k in ("license", "source", "image"):
            if k in t:
                _text(t[k], f"tools.{name}.{k}", limit=200)
        if "commands" in t:
            _texts(t["commands"], f"tools.{name}.commands")
    arts = m["artifacts"]
    if not isinstance(arts, dict) or not arts or not set(arts) <= set(KINDS):
        raise ArtifactRejected("artifacts: archify and/or floorplan only")
    for kind, a in arts.items():
        if kind not in tools:
            raise ArtifactRejected(f"artifacts.{kind}: tool version missing")
        f = "artifacts." + kind
        _keys(a, f, ("sha256", "size", "media_type", "status", "validation", "coverage", "capabilities", "limitations"),
              ("graph",))
        if not SHA256_RE.fullmatch(a["sha256"]):
            raise ArtifactRejected(f + ".sha256: invalid")
        _int(a["size"], f + ".size", 1, MAX_HTML)
        if a["media_type"] != "text/html":
            raise ArtifactRejected(f + ".media_type: text/html only")
        if a["status"] not in STATUSES:
            raise ArtifactRejected(f + ".status: invalid")
        if not isinstance(a["validation"], list) or len(a["validation"]) > MAX_LIST:
            raise ArtifactRejected(f + ".validation: list required")
        for step in a["validation"]:
            _keys(step, f + ".validation[]", ("step", "result"), ("exit", "detail"))
            _text(step["step"], f + ".validation.step", limit=120)
            if step["result"] not in STATUSES:
                raise ArtifactRejected(f + ".validation.result: invalid")
            if "exit" in step:
                _int(step["exit"], f + ".validation.exit", 0, 255)
            if "detail" in step:
                _text(step["detail"], f + ".validation.detail")
        cov = a["coverage"]
        if not isinstance(cov, dict) or len(cov) > MAX_LIST:
            raise ArtifactRejected(f + ".coverage: object required")
        for k, v in cov.items():
            _text(k, f + ".coverage key", limit=80)
            if not (type(v) is int or (isinstance(v, str) and len(v) <= 120)):
                raise ArtifactRejected(f + ".coverage: int or short text values")
        _texts(a["capabilities"], f + ".capabilities")
        _texts(a["limitations"], f + ".limitations")
        if "graph" in a:
            validate_graph(a["graph"], f + ".graph")
    if "limitations" in m:
        _texts(m["limitations"], "limitations")
    return m


def validate_html(data: bytes, kind: str) -> list[str]:
    """Refuse anything that is not a bounded, self-contained, credential-free HTML document.

    Returns non-fatal findings that must be shown as limitations.
    """
    if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_HTML:
        raise ArtifactRejected(f"{kind}: size outside 1..{MAX_HTML} bytes")
    if not DOCTYPE.match(data[:512]):
        raise ArtifactRejected(f"{kind}: not an HTML document")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactRejected(f"{kind}: not UTF-8") from exc
    if any(p.search(data) for p in SECRET_PATTERNS):
        raise ArtifactRejected(f"{kind}: credential-shaped content")
    if REMOTE_LOAD.search(data):
        raise ArtifactRejected(f"{kind}: loads a remote resource; only self-contained output is accepted")
    findings = []
    if HOME_PATH.search(data):
        findings.append("Có đường dẫn thư mục người dùng cục bộ trong HTML.")
    return findings


def read_regular_file(path: Path, limit: int) -> bytes:
    """Open without following symlinks; refuse non-regular or oversized files."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ArtifactRejected(f"{Path(path).name}: cannot open as a regular non-symlink file") from exc
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
        os.close(fd)
        raise ArtifactRejected(f"{Path(path).name}: not a bounded regular file")
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ArtifactRejected(f"{Path(path).name}: grew beyond limit while reading")
    return data


def archify_graph(ir: dict) -> dict:
    comps = ir.get("components") if isinstance(ir, dict) else None
    conns = ir.get("connections") if isinstance(ir, dict) else None
    if not isinstance(comps, list) or not isinstance(conns, list):
        raise ArtifactRejected("archify IR: components/connections missing")
    nodes = [[str(c.get("id", ""))[:200], str(c.get("label", ""))[:200]] for c in comps if isinstance(c, dict)]
    ids = {n[0] for n in nodes}
    edges = sorted(
        {(str(c.get("from")), str(c.get("to")), str(c.get("label", ""))[:200])
         for c in conns if isinstance(c, dict) and c.get("from") in ids and c.get("to") in ids}
    )
    return {"nodes": sorted(nodes), "edges": [list(e) for e in edges]}


def archify_sources(ir: dict) -> list[str]:
    out = set()
    for c in ir.get("components", []):
        for s in c.get("sources", []) if isinstance(c, dict) else []:
            if isinstance(s, dict) and isinstance(s.get("path"), str):
                out.add(s["path"])
    return sorted(out)


def floorplan_graph(graph: dict) -> tuple[dict, dict]:
    """Module-level graph (nodes + aggregated file links) and scanner coverage counts."""
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise ArtifactRejected("floorplan graph: nodes missing")
    nodes = [[str(n.get("id", ""))[:200], str(n.get("label", ""))[:200]] for n in graph["nodes"] if isinstance(n, dict)]
    ids = {n[0] for n in nodes}
    owner = {f.get("p"): f.get("n") for f in graph.get("files", []) if isinstance(f, dict)}

    def module(ref):
        if not isinstance(ref, str):
            return None
        if ref.startswith("n:"):
            return ref[2:] if ref[2:] in ids else None
        if ref.startswith("f:"):
            return owner.get(ref[2:])
        return None

    edges = set()
    for link in graph.get("links", []):
        if not isinstance(link, dict):
            continue
        a, b = module(link.get("a")), module(link.get("b"))
        if a and b and a != b and a in ids and b in ids:
            edges.add((a, b, str(link.get("k", ""))[:200]))
    files = graph.get("files", [])
    described = sum(1 for f in files if isinstance(f, dict) and f.get("d"))
    counts = (graph.get("meta") or {}).get("counts") or {}
    coverage = {
        "files": len(files),
        "files_described": described,
        "modules": len(nodes),
        "unassigned_files": sum(1 for f in files if isinstance(f, dict) and not f.get("n")),
        "links": len(graph.get("links", [])),
        "routes": len(graph.get("routes", [])),
    }
    for k, v in counts.items():
        if isinstance(k, str) and type(v) is int and len(coverage) < MAX_LIST:
            coverage.setdefault("scan_" + k[:60], v)
    return {"nodes": sorted(nodes), "edges": [list(e) for e in sorted(edges)]}, coverage


class ArchitectureStore:
    """Snapshot storage inside the private steward SQLite store."""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        import sqlite3

        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self):
        with closing(self.connect()) as db, db:
            db.executescript(SCHEMA)

    def put(self, manifest: dict, blobs: dict[str, bytes]) -> tuple[str, bool]:
        """Validate and store one immutable snapshot. Returns (id, created)."""
        validate_manifest(manifest)
        findings = {}
        for kind, meta in manifest["artifacts"].items():
            data = blobs.get(kind)
            if data is None:
                raise ArtifactRejected(f"{kind}: artifact bytes missing")
            if sha256(data) != meta["sha256"] or len(data) != meta["size"]:
                raise ArtifactRejected(f"{kind}: hash/size differ from manifest")
            findings[kind] = validate_html(data, kind)
            for note in findings[kind]:
                if note not in meta["limitations"]:
                    raise ArtifactRejected(f"{kind}: undeclared limitation: {note}")
        if set(blobs) - set(manifest["artifacts"]):
            raise ArtifactRejected("artifact bytes without a manifest entry")
        body = canonical(manifest)
        snap = sha256(body.encode())
        self.init()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM arch_snapshots WHERE id=?", (snap,)).fetchone():
                return snap, False
            for kind, data in blobs.items():
                db.execute(
                    "INSERT OR IGNORE INTO arch_blobs VALUES (?,?,?)",
                    (manifest["artifacts"][kind]["sha256"], len(data), zlib.compress(data, 6)),
                )
            db.execute(
                "INSERT INTO arch_snapshots VALUES (?,?,?,?,?,?,?)",
                (snap, manifest["project"], manifest["source"]["commit"], manifest["generated_at"],
                 int(time.time()), manifest["origin"], body),
            )
            self._retain(db, manifest["project"])
        return snap, True

    @staticmethod
    def _retain(db, project):
        """Bounded retention per project; a blob is removed only when no snapshot references it."""
        old = db.execute(
            "SELECT id FROM arch_snapshots WHERE project=? ORDER BY generated_at DESC, imported_at DESC LIMIT -1 OFFSET ?",
            (project, KEEP_SNAPSHOTS),
        ).fetchall()
        if not old:
            return
        db.executemany("DELETE FROM arch_snapshots WHERE id=?", [(r["id"],) for r in old])
        live = set()
        for r in db.execute("SELECT manifest FROM arch_snapshots"):
            live |= {a["sha256"] for a in json.loads(r["manifest"])["artifacts"].values()}
        for r in db.execute("SELECT sha256 FROM arch_blobs").fetchall():
            if r["sha256"] not in live:
                db.execute("DELETE FROM arch_blobs WHERE sha256=?", (r["sha256"],))

    def _has_tables(self, db):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='arch_snapshots'").fetchone())

    def projects(self) -> list[dict]:
        with closing(self.connect()) as db:
            if not self._has_tables(db):
                return []
            rows = db.execute(
                "SELECT project, COUNT(*) n, MAX(generated_at) latest FROM arch_snapshots GROUP BY project ORDER BY project"
            ).fetchall()
        return [dict(r) for r in rows]

    def snapshots(self, project: str) -> list[dict]:
        validate_project(project)
        with closing(self.connect()) as db:
            if not self._has_tables(db):
                return []
            rows = db.execute(
                "SELECT id, imported_at, manifest FROM arch_snapshots WHERE project=? ORDER BY generated_at DESC, imported_at DESC",
                (project,),
            ).fetchall()
        return [dict(id=r["id"], imported_at=r["imported_at"], **json.loads(r["manifest"])) for r in rows]

    def snapshot(self, project: str, snap_id: str) -> dict | None:
        """Lookup is always scoped by project: another project's ID is simply not found."""
        if not SHA256_RE.fullmatch(snap_id or ""):
            return None
        validate_project(project)
        with closing(self.connect()) as db:
            if not self._has_tables(db):
                return None
            r = db.execute(
                "SELECT id, imported_at, manifest FROM arch_snapshots WHERE id=? AND project=?", (snap_id, project)
            ).fetchone()
        return dict(id=r["id"], imported_at=r["imported_at"], **json.loads(r["manifest"])) if r else None

    def artifact(self, project: str, snap_id: str, kind: str, expected_sha: str) -> bytes:
        """Return verified bytes or raise LookupError (not found) / ArtifactRejected (stale/wrong/corrupt)."""
        if kind not in KINDS:
            raise LookupError(kind)
        snap = self.snapshot(project, snap_id)
        if not snap or kind not in snap["artifacts"]:
            raise LookupError(kind)
        meta = snap["artifacts"][kind]
        if expected_sha != meta["sha256"]:
            raise ArtifactRejected("requested hash is not this snapshot's artifact")
        with closing(self.connect()) as db:
            r = db.execute("SELECT body FROM arch_blobs WHERE sha256=?", (meta["sha256"],)).fetchone()
        if not r:
            raise LookupError(kind)
        data = zlib.decompress(r["body"])
        if sha256(data) != meta["sha256"] or len(data) != meta["size"]:
            raise ArtifactRejected("stored artifact failed integrity check")
        return data


def diff_graph(old: dict | None, new: dict | None) -> dict | None:
    if not old or not new:
        return None
    on, nn = {n[0]: n[1] for n in old["nodes"]}, {n[0]: n[1] for n in new["nodes"]}
    oe, ne = {tuple(e) for e in old["edges"]}, {tuple(e) for e in new["edges"]}
    return dict(
        nodes_added=sorted(nn.keys() - on.keys()),
        nodes_removed=sorted(on.keys() - nn.keys()),
        edges_added=sorted(ne - oe),
        edges_removed=sorted(oe - ne),
    )


def history(snaps: list[dict]) -> list[dict]:
    """Newest first; each entry carries a real node/edge diff against the previous snapshot where both have graphs."""
    out = []
    ordered = sorted(snaps, key=lambda s: (s["generated_at"], s["imported_at"]))
    prev = None
    for s in ordered:
        diffs = {}
        for kind in KINDS:
            a = (prev or {}).get("artifacts", {}).get(kind, {}).get("graph")
            b = s["artifacts"].get(kind, {}).get("graph")
            diffs[kind] = diff_graph(a, b) if prev else None
        out.append(dict(snapshot=s, previous=prev and prev["id"], diffs=diffs))
        prev = s
    return list(reversed(out))


def freshness(snap: dict, tracked: dict | None, now: int | None = None) -> dict:
    """Compare the snapshot's commit with the configured tracked ref. Never guesses.

    tracked: {"ref": str, "commit": sha|None, "distance": int|None, "error": str|None}
    """
    now = now or int(time.time())
    age = now - snap["generated_at"]
    if not tracked or tracked.get("error") or not tracked.get("commit"):
        return dict(state="UNKNOWN", reason=(tracked or {}).get("error") or "Chưa cấu hình nhánh theo dõi cho dự án.",
                    age=age)
    if tracked["commit"] == snap["source"]["commit"]:
        state = "CURRENT" if age <= FRESH_SECONDS else "AGED"
        reason = "Cùng commit với " + tracked["ref"] + (
            "." if state == "CURRENT" else "; bản dựng đã quá 7 ngày, nên chạy lại công cụ.")
        return dict(state=state, reason=reason, age=age)
    dist = tracked.get("distance")
    reason = f"{tracked['ref']} đang ở {tracked['commit'][:12]}"
    reason += f", mới hơn {dist} commit." if isinstance(dist, int) and dist > 0 else ", không cùng nhánh lịch sử với snapshot."
    return dict(state="STALE", reason=reason, age=age)


def deployment_view(snap: dict, observations: list[dict]) -> dict:
    """Source-vs-observed revisions from steward inventory, exact SHA equality only."""
    commit = snap["source"]["commit"]
    same, other, runtime = [], [], []
    for r in observations:
        v = r.get("version") or ""
        if r["source"] == "git":
            (same if v == commit else other).append(r)
        elif r["source"] in ("android", "systemd-user", "systemd-system", "docker"):
            runtime.append(dict(r, matches=v == commit, has_sha=bool(SHA1_RE.fullmatch(v))))
    if any(r["matches"] for r in runtime):
        state, reason = "MATCH", "Một dịch vụ đang chạy báo đúng commit của snapshot."
    elif any(r["has_sha"] for r in runtime):
        state, reason = "DIFFERENT", "Dịch vụ đang chạy báo commit khác snapshot."
    elif runtime:
        state, reason = ("UNKNOWN",
                         "Bản đang chạy chỉ báo tên phiên bản, không có SHA hay biên nhận build; không suy đoán commit.")
    else:
        state, reason = "UNKNOWN", "Chưa có quan sát runtime cho dự án này."
    return dict(state=state, reason=reason, same_checkouts=same, other_checkouts=len(other), runtime=runtime)
