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


@pytest.mark.parametrize("evidence", ["missing", "no-browser", "wrong-source", "stale-artifact", "current"])
def test_archify_gates_require_current_input_and_artifact(tmp_path, evidence):
    ir = json.dumps({"meta": {"repository": {"revision": COMMIT}}}).encode()
    (tmp_path / "archify.html").write_bytes(ARCHIFY)
    if evidence != "missing":
        submitted = ir.replace(COMMIT.encode(), b"d" * 40) if evidence == "wrong-source" else ir
        html = FLOORPLAN if evidence == "stale-artifact" else ARCHIFY
        gates = dict(validate="pass", deliver="pass", check="pass")
        if evidence != "no-browser":
            gates["browser-check"] = "pass"
        receipt = dict(specification=dict(sha256=sha256(submitted), bytes=len(submitted)),
                       artifact=dict(sha256=sha256(html), bytes=len(html)), gates=gates)
        (tmp_path / "archify.finalize-summary.json").write_text(json.dumps(receipt))
    result = architecture_rerun.finalize_gates(tmp_path, ir)
    if evidence == "no-browser":
        assert result == {"validate": "pass", "deliver": "pass", "check": "pass", "browser-check": "not-run"}
        limitations = architecture_rerun.archify_limitations("PARTIAL")
        assert limitations == ["Kết quả kiểm tra trình duyệt của Archify chưa đạt PASS trong bằng chứng của lượt chạy này."]
    elif evidence == "current":
        assert result["browser-check"] == "pass"
    else:
        assert result == {}


@pytest.mark.parametrize("configured,state,snapshot", [
    (True, None, False),
    (True, "DISPATCHED", False),
    (True, "FAILED", False),
    (False, None, False),
    (True, "FAILED", True),
])
def test_project_rerun_controls_before_and_after_first_snapshot(
    steward, monkeypatch, configured, state, snapshot,
):
    memory, client = steward
    path = f"/steward/architecture/{PROJECT}"
    monkeypatch.setattr(runs_mod, "load_config", lambda: {"projects": {PROJECT: {}} if configured else {}})
    monkeypatch.setattr(runs_mod, "tracked_revision", lambda *_: {"ref": "main", "commit": COMMIT})
    run = dict(id=51, state=state, created_at=1, cleanup="CLEANUP_PENDING", commit=COMMIT,
               events=[dict(event="RUN_EVENT", at="2026-01-01", evidence={"reason": "synthetic run detail"})])
    monkeypatch.setattr(runs_mod, "runs", lambda _: [run] if state else [])
    if snapshot:
        ArchitectureStore(memory.path).put(manifest(), {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    assert f'action="{path}/regenerate"' not in client.get(path).text
    login(memory, client)
    page = client.get(path)
    assert page.status_code == 200
    assert page.text.count(f'action="{path}/regenerate"') == int(configured)
    assert ("Chưa có snapshot cho dự án này." in page.text) is (not snapshot)
    assert ("Dự án chưa được cấu hình để chạy lại." in page.text) is (not configured)
    if state:
        assert page.text.count(f"Lượt #51 · {state}") == 1
        assert "synthetic run detail" in page.text
    if snapshot:
        viewer = client.get(path + "?tab=archify")
        assert viewer.text.count("<iframe ") == 1
        assert f'action="{path}/regenerate"' not in viewer.text


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
    obs.append(dict(source="systemd-user", name="demo.service", project=PROJECT, state="active", version="d" * 40, location="", node="hp-linux", fresh=True))
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
    for metadata in ({}, {"sec-fetch-dest": "iframe"}, {"sec-fetch-site": "same-origin"}):
        assert client.get(art, headers=metadata).status_code == 403
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
        raise runs_mod.LaunchRefused("no systemd")

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


def test_archify_renderer_gets_only_bounded_filtered_git_objects(tmp_path, monkeypatch):
    from app.services.git_workspace import GitSafetyError

    code = "one\ntwo\nthree\n"
    credential = "ghp_" + "z" * 36
    src = repo_with(tmp_path, "original", {
        "app/a.js": code, "app/.env.js": credential, "app/large.js": "x" * (1 << 20),
    })
    ro = runs_mod.ReadOnlySource(src)
    commit = ro.resolve("HEAD")
    d = tmp_path / "run"
    d.mkdir()
    project = {"include_prefixes": ["app/"], "include_suffixes": [".js"], "exclude_globs": ["*/.env*"]}
    limits = {**runs_mod.DEFAULT_LIMITS, "max_file_bytes": 1024, "max_source_bytes": 4096}
    source = runs_mod.materialize(ro, commit, project, limits, d / "src")
    ir = {"meta": {"repository": {"revision": commit, "url": "https://github.com/example/demo-app"}},
          "components": [{"id": "a", "sources": [{"path": "app/a.js", "line": 2}]}], "connections": []}
    (d / "archify-ir.json").write_text(json.dumps(ir))
    excluded = [obj for _, obj, _, path in ro.tree(commit) if path != "app/a.js"]

    def renderer(cfg, mounts, argv, timeout, *, run, step):
        pin, work = mounts[0][0], mounts[1][0]
        assert mounts == [(pin, "/repo", "ro"), (work, "/work", "rw"), (tmp_path / "archify", "/tools/archify", "ro")]
        assert argv[argv.index("--repo-root") + 1] == "/repo"
        submitted = (work / "ir.json").read_bytes()
        candidate = json.loads(submitted)
        revision = candidate["meta"]["repository"]["revision"]
        assert candidate["meta"]["repository"]["link_mode"] == "local-only"
        assert revision == git(pin, "rev-parse", "HEAD") != commit
        assert git(pin, "rev-list", "--count", "HEAD") == "1"
        assert git(pin, "ls-tree", "-r", "--name-only", revision) == "app/a.js"
        citation = candidate["components"][0]["sources"][0]
        assert git(pin, "show", f"{revision}:{citation['path']}").splitlines()[citation["line"] - 1] == "two"
        assert git(pin, "blame", "-L", "2,2", revision, "--", citation["path"]).endswith("two")
        objects = git(pin, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objectsize)")
        sizes = dict(entry.split() for entry in objects.splitlines())
        assert all(obj not in sizes for obj in [commit, *excluded])
        assert sum(map(int, sizes.values())) <= limits["max_source_bytes"]
        files = [path for path in pin.rglob("*") if path.is_file()]
        assert sum(path.stat().st_size for path in files) <= limits["max_source_bytes"]
        assert all(path.stat().st_nlink == 1 and not path.is_symlink() for path in files)
        assert not (pin / ".git/objects/info/alternates").exists()
        assert not list(pin.rglob("*.promisor"))
        assert git(pin, "config", "--local", "--get", "protocol.allow") == "never"
        assert all(credential.encode() not in path.read_bytes() for path in files)
        (work / "archify.html").write_bytes(ARCHIFY)
        receipt = dict(specification=dict(sha256=sha256(submitted), bytes=len(submitted)),
                       artifact=dict(sha256=sha256(ARCHIFY), bytes=len(ARCHIFY)),
                       gates=dict(validate="pass", deliver="pass", check="pass"))
        (work / "archify.finalize-summary.json").write_text(json.dumps(receipt))
        return dict(exit=1, seconds=0, tail="")

    monkeypatch.setattr(architecture_rerun, "container", renderer)
    cfg = {"tools": {"archify": {"path": str(tmp_path)}}, "limits": limits}
    result = architecture_rerun.run_archify(cfg, d, ro, commit, source)
    assert source["inputCommit"] == commit
    assert result["filtered_revision"] != commit and result["ok"] and result["status"] == "PARTIAL"
    with pytest.raises(GitSafetyError, match="repository exceeds its byte budget"):
        ro.pin_filtered(commit, d / "too-small", {"app/a.js": code.encode()}, len(code))
    git(src, "commit", "--allow-empty", "-qm", "same tree, different source commit")
    other_source = ro.resolve("HEAD")
    assert ro.pin_filtered(other_source, d / "other-source", {"app/a.js": code.encode()}, 4096) != result["filtered_revision"]


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

@pytest.mark.parametrize("source,state,fresh,expected", [
    ("systemd-user", "active", True, "MATCH"),
    ("systemd-system", "inactive", True, "UNKNOWN"),
    ("systemd-user", "active", False, "UNKNOWN"),
    ("systemd-user", "active", None, "UNKNOWN"),
    ("docker", "exited", True, "UNKNOWN"),
    ("docker", "running", True, "MATCH"),
    ("android", "installed", True, "UNKNOWN"),
    ("android", "process-present", True, "MATCH"),
])
def test_runtime_truth_requires_fresh_running_proof(source, state, fresh, expected):
    snap = {"source": {"commit": COMMIT}}
    obs = dict(source=source, name="demo", state=state, version=COMMIT)
    if fresh is not None:
        obs["fresh"] = fresh
    result = deployment_view(snap, [obs])
    assert result["state"] == expected
    assert result["runtime"][0]["matches"] is (expected == "MATCH")


def test_runtime_truth_mixed_versions_do_not_report_match():
    snap = {"source": {"commit": COMMIT}}
    rows = [dict(source="docker", state="running", version=v, fresh=True)
            for v in (COMMIT, "d" * 40)]
    assert deployment_view(snap, rows)["state"] == "DIFFERENT"


def test_runtime_truth_route_distinguishes_stale_and_current(steward):
    memory, client = steward
    ArchitectureStore(memory.path).put(manifest(), {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    login(memory, client)
    sources = dict.fromkeys(SOURCES, "ok")
    row = dict(source="systemd-user", name="demo.service", project=PROJECT,
               state="active", version=COMMIT, location="")
    now = int(time.time())
    memory.ingest(dict(node="hp-linux", at=now-1800, sources=sources, items=[row]))
    path = f"/steward/architecture/{PROJECT}"
    stale = client.get(path)
    assert stale.status_code == 200 and "a-state a-unknown" in stale.text
    assert "Dữ liệu cũ / chưa xác minh" in stale.text
    memory.ingest(dict(node="hp-linux", at=now-10, sources=sources, items=[row]))
    current = client.get(path)
    assert current.status_code == 200 and "a-state a-match" in current.text


def test_manifest_coverage_rejects_credential_shaped_text(tmp_path):
    value = "ghp_" + "z" * 36
    bad = manifest()
    bad["artifacts"]["archify"]["coverage"]["notes"] = value
    store = ArchitectureStore(tmp_path / "coverage.db")
    with pytest.raises(ArtifactRejected, match="credential") as exc:
        store.put(bad, {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    assert value not in str(exc.value)
    assert store.snapshots(PROJECT) == []


def test_manifest_unknown_key_error_does_not_echo_value(tmp_path):
    key = "ghp_" + "z" * 36
    bad = manifest()
    bad[key] = True
    with pytest.raises(ArtifactRejected, match="not allowed") as exc:
        ArchitectureStore(tmp_path / "keys.db").put(bad, {"archify": ARCHIFY, "floorplan": FLOORPLAN})
    assert key not in str(exc.value)

def test_global_architecture_admission_is_atomic(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    _, _, cfg_path = gated_config(tmp_path)
    cfg = runs_mod.load_config(cfg_path)
    cfg["projects"][OTHER] = dict(cfg["projects"][PROJECT])
    state = tmp_path / "state"
    entered, release = Event(), Event()
    launched = []
    real_create = runs_mod.HarnessStore.create

    def pause_first(self, **kwargs):
        if kwargs["main_goal"] == runs_mod.MAIN_GOAL + PROJECT:
            entered.set()
            assert release.wait(10)
        return real_create(self, **kwargs)

    monkeypatch.setattr(runs_mod.HarnessStore, "create", pause_first)

    def request(project):
        try:
            return runs_mod.request_rerun(project, cfg=cfg, state=state,
                                          launcher=lambda *args: launched.append(args))
        except runs_mod.RunRefused:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request, PROJECT)
        try:
            assert entered.wait(10)
            second = pool.submit(request, OTHER)
            second_result = second.result(timeout=10)
        finally:
            release.set()
        first_result = first.result(timeout=10)
    assert sum(x is not None for x in (first_result, second_result)) == 1
    assert len(launched) == len(runs_mod.harness(state).list_tasks()) == 1

    # A terminal task without independently verified cleanup still reserves the slot.
    task = runs_mod.harness(state).list_tasks()[0]
    store = runs_mod.harness(state)
    store.fail(task.id, at=datetime.now(timezone.utc), reason="synthetic failure")
    assert request(PROJECT if task.main_goal.endswith(OTHER) else OTHER) is None
    store.cleanup(task.id, at=datetime.now(timezone.utc), executor_stopped=True,
                  no_active_refs=True, dirty_worktree=False, evidence_preserved=True,
                  deletion_verified=True)
    monkeypatch.setattr(runs_mod.HarnessStore, "create", real_create)
    assert request(PROJECT if task.main_goal.endswith(OTHER) else OTHER) is not None

def test_ambiguous_architecture_launch_keeps_capacity(tmp_path):
    _, _, path = gated_config(tmp_path)
    cfg = runs_mod.load_config(path)
    state = tmp_path / "state"
    calls = []
    def timeout(*args):
        calls.append(args)
        raise subprocess.TimeoutExpired("systemd-run", 30)
    with pytest.raises(runs_mod.RunRefused):
        runs_mod.request_rerun(PROJECT, cfg=cfg, state=state, launcher=timeout)
    task = runs_mod.harness(state).list_tasks()[0]
    assert task.state == "FAILED" and task.cleanup_state == "CLEANUP_PENDING"
    with pytest.raises(runs_mod.RunRefused):
        runs_mod.request_rerun(PROJECT, cfg=cfg, state=state, launcher=timeout)
    assert len(calls) == 1


def test_retention_never_erases_active_or_unknown_evidence(tmp_path):
    state = tmp_path / "state"
    for i in range(1, 5):
        d = runs_mod.run_dir(i, state)
        d.mkdir(parents=True)
        (d / "request.json").write_text("{}")
    runs_mod.prune_run_dirs(state, 1)
    assert all((runs_mod.run_dir(i, state) / "request.json").is_file() for i in range(1, 5))

def _terminal_architecture_fixture(tmp_path):
    _, _, path = gated_config(tmp_path)
    state = tmp_path / "state"
    result = runs_mod.request_rerun(PROJECT, cfg=runs_mod.load_config(path), state=state, launcher=lambda *a: None)
    h = runs_mod.harness(state)
    task = h.fail(result["task_id"], at=datetime.now(timezone.utc), reason="synthetic exit")
    d = runs_mod.run_dir(task.id, state)
    req = runs_mod.read_receipt(d / "request.json")
    runs_mod.write_receipt(d / "worker-finished.json", {"owner": req["owner"], "state": task.state, "exit": 1})
    (d / "src").mkdir()
    (d / "src" / "data").write_text("derived source")
    (d / "test.log").write_text("evidence")
    return state, task, d, req


def test_cleanup_requires_independent_proof_and_preserves_evidence(tmp_path, monkeypatch):
    state, task, d, req = _terminal_architecture_fixture(tmp_path)
    monkeypatch.setattr(runs_mod, "runtime_absent", lambda req: False)
    assert not runs_mod.reconcile_run_cleanup(task.id, state)
    assert (d / "src" / "data").is_file()
    assert runs_mod.harness(state).get(task.id).cleanup_state == "CLEANUP_PENDING"
    monkeypatch.setattr(runs_mod, "runtime_absent", lambda req: True)
    with runs_mod.execution_lock(d):
        assert not runs_mod.reconcile_run_cleanup(task.id, state)
    marker = d / "container-test.json"
    runs_mod.write_receipt(marker, {"owner": req["owner"], "state": "UNCERTAIN"})
    assert not runs_mod.reconcile_run_cleanup(task.id, state)
    assert (d / "src").exists()
    runs_mod.write_receipt(marker, {"owner": req["owner"], "state": "REMOVED"})
    assert runs_mod.reconcile_run_cleanup(task.id, state)
    assert not (d / "src").exists()
    for name in ("request.json", "launch.json", "worker-finished.json", "test.log", "container-test.json", "cleanup.json"):
        assert (d / name).is_file()
    assert runs_mod.harness(state).get(task.id).cleanup_state == "CLEANED_VERIFIED"
    assert runs_mod.reconcile_run_cleanup(task.id, state)


@pytest.mark.parametrize("mode", ["active-unit", "missing-property", "docker-error", "container-present", "timeout", "absent"])
def test_runtime_absence_fails_closed(monkeypatch, mode):
    from types import SimpleNamespace
    values = "LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\nJob=0\n"
    def probe(argv, **kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 10)
        if argv[0] == "systemctl":
            text = values.replace("ActiveState=inactive", "ActiveState=active") if mode == "active-unit" else values
            if mode == "missing-property":
                text = text.replace("Job=0\n", "")
            return SimpleNamespace(returncode=0, stdout=text)
        assert argv[:3] == ["docker", "ps", "-aq"]
        assert argv[-1] == "label=projectflow.architecture-owner=" + "a"*32
        return SimpleNamespace(returncode=int(mode == "docker-error"), stdout="cid" if mode == "container-present" else "")
    monkeypatch.setattr(runs_mod.subprocess, "run", probe)
    assert runs_mod.runtime_absent({"unit": "synthetic", "owner": "a"*32}) is (mode == "absent")


def test_cleanup_refuses_legacy_or_ambiguous_launch(tmp_path, monkeypatch):
    state, task, d, req = _terminal_architecture_fixture(tmp_path)
    monkeypatch.setattr(runs_mod, "runtime_absent", lambda req: True)
    runs_mod.write_receipt(d / "launch.json", {"owner": req["owner"], "state": "AMBIGUOUS"})
    assert not runs_mod.reconcile_run_cleanup(task.id, state)
    (d / "launch.json").unlink()
    assert not runs_mod.reconcile_run_cleanup(task.id, state)
    assert (d / "src" / "data").is_file()
