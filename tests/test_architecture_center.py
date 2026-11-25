"""Architecture Center (#91): storage, validation, auth, isolation, gate.

All artifacts here are synthetic. Real pilot outputs stay in the private
steward store and are verified on the deployed private route, never committed.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services import architecture_runs as runs_mod
from app.services.architecture_center import (
    SCHEMA_VERSION,
    ArchitectureStore,
    ArtifactRejected,
    archify_graph,
    deployment_view,
    floorplan_graph,
    freshness,
    history,
    read_regular_file,
    sha256,
    utc_iso,
    validate_html,
)
from app.services.fleet_steward import SOURCES, Memory
from scripts import architecture_rerun

PROJECT = "example/demo-app"
OTHER = "example/other-app"
COMMIT = "a" * 40
COOKIE = "__Host-pf-steward"
ARCHIFY = b"<!doctype html><html><head><title>Demo runtime map Diagram</title></head><body><svg></svg><script>document.title+='';</script></body></html>"
FLOORPLAN = b"<!doctype html><html><head><title>Demo map</title><style>body{}</style></head><body><input id=q><script>location.hash='#s=n%3Aapi'</script></body></html>"


def graph(nodes, edges):
    return {"nodes": [[n, n.upper()] for n in nodes], "edges": [[a, b, "import"] for a, b in edges]}


def manifest(project=PROJECT, commit=COMMIT, archify=ARCHIFY, floorplan=FLOORPLAN, at=None, fp_graph=None):
    def art(data, g):
        return dict(sha256=sha256(data), size=len(data), media_type="text/html", status="PARTIAL",
                    validation=[dict(step="render", result="PASS", exit=0)], coverage={"files": 3, "files_described": 0},
                    capabilities=["search"], limitations=["synthetic"], graph=g)

    return dict(
        schema=SCHEMA_VERSION, project=project,
        source=dict(type="git-commit", commit=commit, committed_at="2026-10-31T17:00:00Z", branch="main"),
        generated_at=at or int(time.time()) - 60, origin="imported-pilot",
        provenance=dict(summary="synthetic fixture", operator="test"),
        tools=dict(archify=dict(version="v3.3.0", commit="b" * 40), floorplan=dict(version="0.11.0", commit="c" * 40)),
        artifacts=dict(archify=art(archify, graph(["ui", "api"], [("ui", "api")])),
                       floorplan=art(floorplan, fp_graph or graph(["api", "db"], [("api", "db")]))),
    )


@pytest.fixture()
def steward(tmp_path, monkeypatch):
    m = Memory(tmp_path / "steward.db")
    m.init()
    monkeypatch.setenv("PROJECTFLOW_STEWARD_DB", str(m.path))
    monkeypatch.setenv("PROJECTFLOW_STEWARD_KEYS", str(tmp_path / "keys.json"))
    monkeypatch.setenv("PROJECTFLOW_ARCHITECTURE_CONFIG", str(tmp_path / "missing-config.json"))
    root = tmp_path / "repo"
    root.mkdir()
    app = create_app(Settings(root, "127.0.0.1", 8765, tmp_path / "pf.db", 10))
    return m, TestClient(app, base_url="https://testserver")


def login(m, client):
    session = m.redeem(m.issue())
    client.cookies.set(COOKIE, session)
    return session


IFRAME = {"sec-fetch-dest": "iframe", "sec-fetch-site": "same-origin"}


def test_snapshot_is_immutable_content_addressed_and_survives_restart(tmp_path):
    store = ArchitectureStore(tmp_path / "s.db")
    snap, created = store.put(manifest(), {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    assert created
    again, created_again = store.put(manifest(at=store.snapshot(PROJECT, snap)["generated_at"]), {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    assert again == snap and not created_again
    reopened = ArchitectureStore(tmp_path / "s.db")  # process restart = new handle on the same file
    assert reopened.artifact(PROJECT, snap, "floorplan", sha256(FLOORPLAN)) == FLOORPLAN
    assert reopened.snapshot(OTHER, snap) is None  # project scoping
    with pytest.raises(ArtifactRejected):
        reopened.artifact(PROJECT, snap, "archify", sha256(FLOORPLAN))  # wrong/stale hash
    with pytest.raises(LookupError):
        reopened.artifact(PROJECT, snap, "../archify", sha256(ARCHIFY))


@pytest.mark.parametrize("bad, reason", [
    (b"<html><script>1</script></html>", "not an HTML document"),
    (b"<!doctype html><script src=https://cdn.example/x.js></script>", "remote resource"),
    (b"<!doctype html><link rel=stylesheet href='//cdn.example/x.css'>", "remote resource"),
    (b"<!doctype html><img src=\"http://track.example/p.gif\">", "remote resource"),
    (b"<!doctype html><p>ghp_" + b"a" * 36 + b"</p>", "credential"),
    (b"<!doctype html>-----BEGIN PRIVATE KEY-----", "credential"),
    (b"<!doctype html>\xff\xfe", "UTF-8"),
])
def test_untrusted_html_is_refused(bad, reason):
    with pytest.raises(ArtifactRejected, match=reason):
        validate_html(bad, "x")


def test_manifest_and_hash_mismatch_are_refused(tmp_path):
    store = ArchitectureStore(tmp_path / "s.db")
    with pytest.raises(ArtifactRejected, match="hash/size"):
        store.put(manifest(), {"archify": ARCHIFY + b" ", "floorplan": FLOORPLAN})
    bad = manifest()
    bad["source"]["commit"] = "main"
    with pytest.raises(ArtifactRejected, match="40-hex"):
        store.put(bad, {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    bad = manifest(project="../etc")
    with pytest.raises(ArtifactRejected, match="project"):
        store.put(bad, {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    bad = manifest()
    bad["provenance"]["summary"] = "token sk-ant-" + "x" * 40
    with pytest.raises(ArtifactRejected, match="credential"):
        store.put(bad, {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    bad = manifest()
    bad["artifacts"]["archify"]["extra"] = 1
    with pytest.raises(ArtifactRejected, match="not allowed"):
        store.put(bad, {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    assert ArchitectureStore(tmp_path / "s.db").snapshots(PROJECT) == []


def test_import_refuses_symlink_and_oversize(tmp_path):
    real = tmp_path / "real.html"
    real.write_bytes(ARCHIFY)
    link = tmp_path / "link.html"
    link.symlink_to(real)
    with pytest.raises(ArtifactRejected):
        read_regular_file(link, 1 << 20)
    with pytest.raises(ArtifactRejected):
        read_regular_file(real, 10)
    with pytest.raises(ArtifactRejected):
        read_regular_file(tmp_path, 1 << 20)


def test_history_diff_freshness_and_deployment_never_guess(tmp_path):
    store = ArchitectureStore(tmp_path / "s.db")
    old, _ = store.put(manifest(at=1_700_000_000), {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    fp2 = FLOORPLAN.replace(b"Demo map", b"Demo map 2")
    new, _ = store.put(manifest(commit="d" * 40, floorplan=fp2, at=1_700_000_100,
                                fp_graph=graph(["api", "db", "queue"], [("api", "queue")])),
                       {"archify": ARCHIFY, "floorplan": fp2})
    h = history(store.snapshots(PROJECT))
    assert [x["snapshot"]["id"] for x in h] == [new, old]
    d = h[0]["diffs"]["floorplan"]
    assert d["nodes_added"] == ["queue"] and d["edges_removed"] == [("api", "db", "import")]
    assert h[0]["diffs"]["archify"]["nodes_added"] == [] and h[1]["previous"] is None
    snap = store.snapshot(PROJECT, new)
    assert freshness(snap, None)["state"] == "UNKNOWN"
    assert freshness(snap, {"ref": "main", "commit": "d" * 40}, now=1_700_000_200)["state"] == "CURRENT"
    stale = freshness(snap, {"ref": "main", "commit": "e" * 40, "distance": 3})
    assert stale["state"] == "STALE" and "3 commit" in stale["reason"]
    obs = [dict(source="android", name="phone/app", project=PROJECT, state="installed", version="1.0-v9", location="", node="m910"),
           dict(source="git", name="/w/demo", project=PROJECT, state="checkout", version="d" * 40, location="", node="dell-linux")]
    dep = deployment_view(snap, obs)
    assert dep["state"] == "UNKNOWN" and len(dep["same_checkouts"]) == 1  # versionName is never mapped to a SHA
    obs.append(dict(source="systemd-user", name="demo.service", project=PROJECT, state="active", version="d" * 40, location="", node="hp-linux"))
    assert deployment_view(snap, obs)["state"] == "MATCH"


def test_graph_extractors_use_real_structures():
    g = archify_graph({"components": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                       "connections": [{"from": "a", "to": "b", "label": "calls"}, {"from": "a", "to": "zzz"}]})
    assert g["edges"] == [["a", "b", "calls"]]
    fg, cov = floorplan_graph({"nodes": [{"id": "api", "label": "API"}, {"id": "db", "label": "DB"}],
                               "files": [{"p": "a.js", "n": "api", "d": "x"}, {"p": "b.js", "n": "db", "d": ""}],
                               "links": [{"a": "f:a.js", "b": "f:b.js", "k": "import"}], "routes": []})
    assert fg["edges"] == [["api", "db", "import"]] and cov["files_described"] == 1 and cov["routes"] == 0


def test_private_routes_auth_isolation_and_cross_project(steward):
    m, client = steward
    store = ArchitectureStore(m.path)
    snap, _ = store.put(manifest(), {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    other, _ = store.put(manifest(project=OTHER), {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    art = f"/steward/architecture/{PROJECT}/artifact/{snap}/archify?sha={sha256(ARCHIFY)}"
    # Unauthenticated: no data, no artifact, no rerun.
    assert client.get(art, headers=IFRAME).status_code == 401
    page = client.get(f"/steward/architecture/{PROJECT}")
    assert page.status_code == 200 and snap not in page.text and "Demo" not in page.text
    assert PROJECT not in client.get("/steward/architecture").text
    assert client.post(f"/steward/architecture/{PROJECT}/regenerate").status_code == 401
    client.cookies.set(COOKIE, "x" * 43)
    assert client.get(art, headers=IFRAME).status_code == 401
    login(m, client)
    ok = client.get(art, headers=IFRAME)
    assert ok.status_code == 200 and ok.content == ARCHIFY
    csp = ok.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts;") and "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp and "frame-ancestors 'self'" in csp and "default-src 'none'" in csp
    assert ok.headers["x-content-type-options"] == "nosniff" and ok.headers["cache-control"] == "private, no-store"
    # Never as a top-level document, cross-site embed or script fetch.
    assert client.get(art, headers={"sec-fetch-dest": "document", "sec-fetch-site": "none"}).status_code == 403
    assert client.get(art, headers={"sec-fetch-dest": "iframe", "sec-fetch-site": "cross-site"}).status_code == 403
    assert client.get(art, headers={"sec-fetch-dest": "empty", "sec-fetch-site": "same-origin"}).status_code == 403
    # Stale / wrong hash, unknown kind, cross-project and traversal attempts.
    assert client.get(art.replace(sha256(ARCHIFY), sha256(FLOORPLAN)), headers=IFRAME).status_code == 409
    assert client.get(art.replace("/archify?", "/notes?"), headers=IFRAME).status_code == 404
    assert client.get(f"/steward/architecture/{PROJECT}/artifact/{other}/archify?sha={sha256(ARCHIFY)}", headers=IFRAME).status_code == 404
    assert client.get(f"/steward/architecture/{OTHER}/artifact/{snap}/archify?sha={sha256(ARCHIFY)}", headers=IFRAME).status_code == 404
    for evil in (f"/steward/architecture/{PROJECT}/artifact/..%2F..%2Fetc/passwd?sha=x",
                 f"/steward/architecture/{PROJECT}/artifact/{snap}/..%2Farchify?sha={sha256(ARCHIFY)}",
                 "/steward/architecture/..%2F..%2F/x"):
        assert client.get(evil, headers=IFRAME).status_code in (400, 404)
    assert client.get(f"/steward/architecture/{PROJECT}?snapshot={other}").status_code == 404
    # Authorized pages: sandboxed iframe only, viewer CSP forbids off-origin frame navigation.
    viewer = client.get(f"/steward/architecture/{PROJECT}?tab=archify")
    assert viewer.status_code == 200 and 'sandbox="allow-scripts"' in viewer.text and "allow-same-origin" not in viewer.text
    assert "frame-src 'self'" in viewer.headers["content-security-policy"]
    assert ARCHIFY.decode() not in viewer.text  # generated HTML is never inlined
    for tab in ("overview", "floorplan", "history", "evidence"):
        r = client.get(f"/steward/architecture/{PROJECT}?tab={tab}")
        assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    assert client.get(f"/steward/architecture/{PROJECT}?tab=../x").status_code == 404
    assert snap in client.get(f"/steward/architecture/{PROJECT}?tab=evidence").text
    assert PROJECT in client.get("/steward/architecture").text
    # Rerun requires same origin and a configured project; refusal is explicit, nothing starts.
    assert client.post(f"/steward/architecture/{PROJECT}/regenerate", headers={"origin": "https://evil.test"}).status_code == 403
    assert client.post(f"/steward/architecture/{PROJECT}/regenerate", headers={"origin": "null"}).status_code == 403
    r = client.post(f"/steward/architecture/{PROJECT}/regenerate", data={"commit": "; rm -rf /"}, follow_redirects=False)
    assert r.status_code == 303 and "T%E1%BB%AB%20ch%E1%BB%91i" in r.headers["location"]


def test_steward_inventory_history_is_untouched_by_architecture_store(steward):
    m, _ = steward
    sources = dict.fromkeys(SOURCES, "not-configured")
    sources["git"] = "ok"
    now = int(time.time())
    m.ingest(dict(node="dell-linux", at=now - 30, sources=sources,
                  items=[dict(source="git", name="/w/demo", project=PROJECT, state="checkout", version=COMMIT, location="/w/demo")]))
    before = m.report("dell-linux", now)
    ArchitectureStore(m.path).put(manifest(), {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    assert Memory(m.path).report("dell-linux", now) == before
    assert Memory(m.path).changes("dell-linux", 0, now, lambda r: True)[0]["event"] == "Lần đầu thấy"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}).stdout.strip()


def repo_with(tmp_path, name, files):
    r = tmp_path / name
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    for p, text in files.items():
        (r / p).parent.mkdir(parents=True, exist_ok=True)
        (r / p).write_text(text)
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r


def gated_config(tmp_path):
    src = repo_with(tmp_path, "src", {"app/a.js": "export const a = 1\n", "app/b.js": "import {a} from './a.js'\n",
                                       "secrets/.env": "TOKEN=x\n"})
    tools = {k: repo_with(tmp_path, k, {"README": k}) for k in ("archify", "floorplan")}
    ir = tmp_path / "ir.json"
    ir.write_text(json.dumps({"meta": {"repository": {"revision": git(src, "rev-parse", "HEAD")}}, "components": []}))
    cfg = {
        "projects": {PROJECT: {"repo": str(src), "ref": "main", "include_prefixes": ["app/"], "include_suffixes": [".js"],
                               "archify_ir": str(ir)}},
        "tools": {**{k: {"path": str(v), "commit": git(v, "rev-parse", "HEAD"), "version": "pinned"} for k, v in tools.items()},
                  "runtime": {"image": "example/node@sha256:" + "0" * 64}},
        "limits": {"max_active": 1, "min_free_gib": 0},
    }
    path = tmp_path / "architecture.json"
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    return src, tools, path


def test_rerun_gate_dedupes_bounds_and_never_takes_browser_commands(tmp_path, monkeypatch):
    src, tools, cfg_path = gated_config(tmp_path)
    state = tmp_path / "state"
    launched = []
    cfg = runs_mod.load_config(cfg_path)
    first = runs_mod.request_rerun(PROJECT, cfg=cfg, state=state, launcher=lambda *a: launched.append(a))
    assert first["state"] == "DISPATCHED" and not first["duplicate"] and len(launched) == 1
    unit = launched[0][1]
    assert unit.startswith(f"pf-arch-run-{first['task_id']}-") and unit.endswith(first["commit"][:10])
    again = runs_mod.request_rerun(PROJECT, first["commit"], cfg=cfg, state=state, launcher=lambda *a: launched.append(a))
    assert again["duplicate"] and again["task_id"] == first["task_id"] and len(launched) == 1
    (src / "app/a.js").write_text("export const a = 2\n")
    git(src, "commit", "-qam", "next")
    with pytest.raises(runs_mod.RunRefused, match="giới hạn"):
        runs_mod.request_rerun(PROJECT, cfg=cfg, state=state, launcher=lambda *a: launched.append(a))
    for bad in ("main; rm -rf /", "HEAD", "../../etc", "f" * 40):
        with pytest.raises(runs_mod.RunRefused):
            runs_mod.request_rerun(PROJECT, bad, cfg=cfg, state=state, launcher=lambda *a: launched.append(a))
    with pytest.raises(runs_mod.RunRefused, match="chưa được cấu hình"):
        runs_mod.request_rerun(OTHER, cfg=cfg, state=state)
    assert len(launched) == 1
    listed = runs_mod.runs(PROJECT, state)
    assert listed[0]["id"] == first["task_id"] and listed[0]["events"][-1]["event"] == "DISPATCHED"
    # A tool checkout that moved off its pin, or dirtied, blocks every run.
    (tools["floorplan"] / "README").write_text("changed")
    with pytest.raises(runs_mod.RunRefused, match="floorplan"):
        runs_mod.verify_tools(cfg)


def test_launch_failure_is_recorded_not_retried(tmp_path):
    _, _, cfg_path = gated_config(tmp_path)
    cfg = runs_mod.load_config(cfg_path)

    def boom(*a):
        raise RuntimeError("no systemd")

    with pytest.raises(runs_mod.RunRefused):
        runs_mod.request_rerun(PROJECT, cfg=cfg, state=tmp_path / "st", launcher=boom)
    run = runs_mod.runs(PROJECT, tmp_path / "st")[0]
    assert run["state"] == "FAILED" and run["cleanup"] == "CLEANED_VERIFIED"
    assert any("launch failed" in (e["evidence"].get("reason") or "") for e in run["events"])
    # A failed attempt may be retried as a new visible child goal, at most MAX_ATTEMPTS times in total.
    for _ in range(runs_mod.MAX_ATTEMPTS - 1):
        with pytest.raises(runs_mod.RunRefused, match="Không khởi chạy"):
            runs_mod.request_rerun(PROJECT, cfg=cfg, state=tmp_path / "st", launcher=boom)
    with pytest.raises(runs_mod.RunRefused, match="Đã thử 3 lần"):
        runs_mod.request_rerun(PROJECT, cfg=cfg, state=tmp_path / "st", launcher=lambda *a: None)
    goals = [t.child_goal for t in runs_mod.harness(tmp_path / "st").list_tasks()]
    assert len(goals) == 3 and goals[1].endswith("#retry-1") and goals[2].endswith("#retry-2")


def test_systemd_unit_limits_are_valid(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(runs_mod.subprocess, "run", lambda argv, **k: seen.setdefault("argv", argv) and type("P", (), {"returncode": 0, "stderr": ""})())
    runs_mod.launch_unit(7, "pf-arch-run-7-abc", {"limits": {**runs_mod.DEFAULT_LIMITS, "memory": "3g"}})
    argv = seen["argv"]
    assert argv[:3] == ["systemd-run", "--user", "--collect"] and "--property=MemoryMax=3G" in argv
    assert argv[-6:] == ["-m", "scripts.architecture_rerun", "run", "--task-id", "7"][-6:] or argv[-5:] == ["-m", "scripts.architecture_rerun", "run", "--task-id", "7"]


def test_source_allowlist_refuses_symlinks_and_secrets(tmp_path):
    src = repo_with(tmp_path, "s", {"app/a.js": "1", "app/.env.js": "2", "docs/x.js": "3"})
    (src / "app/link.js").symlink_to(src / "app/a.js")
    git(src, "add", ".")
    git(src, "commit", "-qm", "link")
    ro = runs_mod.ReadOnlySource(src)
    p = {"include_prefixes": ["app/"], "include_suffixes": [".js"], "exclude_globs": ["*/.env*"]}
    chosen, excluded = runs_mod.select_sources(ro.tree("HEAD"), p, runs_mod.DEFAULT_LIMITS)
    assert [c[2] for c in chosen] == ["app/a.js"] and sorted(excluded) == ["app/.env.js", "app/link.js"]
    out = tmp_path / "out"
    manifest_ = runs_mod.materialize(ro, ro.resolve("HEAD"), p, runs_mod.DEFAULT_LIMITS, out)
    assert manifest_["includedCount"] == 1 and not (out / "docs").exists() and not (out / ".git").exists()
    runs_mod.make_read_only(out, writable_dirs=(out,))
    mode = lambda q: (out / q).stat().st_mode & 0o777  # mode bits, not os.access: CI runs as root
    assert mode("app") == 0o555 and mode("app/a.js") == 0o444 and mode(".") & 0o200
    assert git(src, "status", "--porcelain") == ""  # source repo untouched


def test_carried_forward_ir_with_moved_citations_is_refused(tmp_path):
    src = repo_with(tmp_path, "s", {"app/a.js": "one\ntwo\nthree\n"})
    base = git(src, "rev-parse", "HEAD")
    ir = {"meta": {"repository": {"revision": base}}, "components": [{"id": "a", "sources": [{"path": "app/a.js", "line": 2, "end_line": 2}]}]}
    ro = runs_mod.ReadOnlySource(src)
    (src / "app/a.js").write_text("one\ntwo\nthree\nfour\n")
    git(src, "commit", "-qam", "append")
    assert architecture_rerun.cited_drift(ro, ir, ro.resolve("HEAD")) == []  # cited lines unchanged
    (src / "app/a.js").write_text("zero\none\ntwo\nthree\nfour\n")
    git(src, "commit", "-qam", "shift")
    assert architecture_rerun.cited_drift(ro, ir, ro.resolve("HEAD")) == ["a:app/a.js:2-2"]


def test_config_permissions_and_pins_are_enforced(tmp_path):
    _, _, cfg_path = gated_config(tmp_path)
    cfg_path.chmod(0o644)
    with pytest.raises(runs_mod.RunRefused, match="0600"):
        runs_mod.load_config(cfg_path)
    cfg_path.chmod(0o600)
    data = json.loads(cfg_path.read_text())
    data["tools"]["runtime"]["image"] = "node:latest"
    cfg_path.write_text(json.dumps(data))
    with pytest.raises(runs_mod.RunRefused, match="digest"):
        runs_mod.load_config(cfg_path)
    assert runs_mod.load_config(tmp_path / "absent.json")["projects"] == {}


def test_harness_ttl_expiry_is_visible(tmp_path):
    _, _, cfg_path = gated_config(tmp_path)
    cfg = runs_mod.load_config(cfg_path)
    past = datetime(2026, 1, 1, tzinfo=timezone.utc)
    r = runs_mod.request_rerun(PROJECT, cfg=cfg, state=tmp_path / "st", launcher=lambda *a: None, now=past)
    run = runs_mod.runs(PROJECT, tmp_path / "st")[0]
    assert run["id"] == r["task_id"] and run["state"] == "EXPIRED" and run["cleanup"] == "CLEANUP_PENDING"


def test_committed_at_is_canonical_utc(tmp_path):
    assert utc_iso("2026-11-06T21:15:51+07:00") == "2026-11-06T14:15:51Z"
    assert utc_iso("2026-11-06T14:15:51Z") == "2026-11-06T14:15:51Z"
    for bad in ("2026-11-06T21:15:51", "yesterday", "", "2026-13-01T00:00:00Z"):
        with pytest.raises(ArtifactRejected):
            utc_iso(bad)
    store = ArchitectureStore(tmp_path / "s.db")
    for bad in ("2026-11-06T21:15:51+07:00", "2026-11-06T14:15:51+00:00", "2026-11-06T14:15:51.5Z", "2026-11-06 14:15:51Z"):
        m = manifest()
        m["source"]["committed_at"] = bad
        with pytest.raises(ArtifactRejected, match="canonical UTC"):
            store.put(m, {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    snap, _ = store.put(manifest(), {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    assert store.snapshot(PROJECT, snap)["source"]["committed_at"].endswith("Z")
