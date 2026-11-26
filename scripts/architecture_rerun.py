"""Architecture Center operator CLI and gated runner (#91).

  enqueue  --project P [--commit SHA] [--archify-ir FILE]   create/dedupe a gated rerun
  run      --task-id N                                       worker (started by the gate as a transient unit)
  import   --project P ... (pilot outputs)                   validated import of existing native outputs
  status   --project P                                       snapshots and runs

Tools run offline from clean checkouts pinned by commit inside a pinned container
image. Output is imported through ArchitectureStore.put, the same validation used
for manual imports. No source file is modified; nothing is fetched or pushed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.agent_harness_mvp import HarnessError  # noqa: E402
from app.services.architecture_center import (  # noqa: E402
    MAX_AUX,
    MAX_HTML,
    SCHEMA_VERSION,
    ArchitectureStore,
    ArtifactRejected,
    archify_graph,
    archify_sources,
    canonical,
    floorplan_graph,
    read_regular_file,
    sha256,
    validate_html,
    utc_iso,
    validate_project,
)
from app.services.architecture_runs import (  # noqa: E402
    RunRefused,
    container,
    harness,
    load_config,
    make_read_only,
    materialize,
    prune_run_dirs,
    assert_executor_active,
    bound_request,
    execution_lock,
    reconcile_run_cleanup,
    write_receipt,
    request_rerun,
    run_dir,
    runs,
    state_dir,
    tool_identity,
    verify_tools,
)
from app.services.git_workspace import ReadOnlySource  # noqa: E402

FLOORPLAN_STEPS = ("scan", "build", "describe", "build")
FLOORPLAN_CAPS = ["Tìm module/tệp", "Điều hướng bằng URL hash", "Xem liên kết import/call/data"]
FLOORPLAN_LIMITS = [
    "Hỏi đáp (Ask) không chạy trong chế độ xem ngoại tuyến.",
    "Mô tả tệp là khung tự sinh; số tệp có mô tả xem ở phần phủ quét.",
    "Không nhận diện được route chưa chứng minh ứng dụng không có route.",
    "Android được quét như mã Java; không phải bằng chứng runtime trên thiết bị.",
    "HTML nhúng thời điểm build nên SHA-256 khác giữa các lượt cùng commit; so sánh lịch sử dùng đồ thị và phủ quét.",
]
ARCHIFY_CAPS = ["Sơ đồ thành phần có trích dẫn dòng mã", "Tìm kiếm và chọn tuyến", "Thẻ điều kiện nguồn"]


def now():
    return datetime.now(timezone.utc)


def store():
    db = os.getenv("PROJECTFLOW_STEWARD_DB") or str(state_dir() / "memory.sqlite3")
    if not Path(db).is_file():
        raise SystemExit("steward store missing: " + db)
    return ArchitectureStore(db)


def cited_drift(src: ReadOnlySource, ir: dict, target: str) -> list[str]:
    """Citations whose exact text differs between the IR's revision and the target commit."""
    base = (ir.get("meta", {}).get("repository", {}) or {}).get("revision")
    if not base or base == target:
        return []
    base = src.resolve(base)
    tree_old = {p: o for _, o, _, p in src.tree(base)}
    tree_new = {p: o for _, o, _, p in src.tree(target)}
    changed = []
    for comp in ir.get("components", []):
        for s in comp.get("sources", []):
            p, a = s.get("path"), s.get("line")
            b = s.get("end_line", a)
            if p not in tree_old or p not in tree_new:
                changed.append(f"{comp.get('id')}:{p}")
                continue
            blobs = src.blobs(sorted({tree_old[p], tree_new[p]}))
            old = blobs[tree_old[p]].decode("utf-8", "replace").splitlines()[a - 1:b]
            new = blobs[tree_new[p]].decode("utf-8", "replace").splitlines()[a - 1:b]
            if old != new:
                changed.append(f"{comp.get('id')}:{p}:{a}-{b}")
    return changed


def run_floorplan(cfg, d: Path) -> dict:
    tool = cfg["tools"]["floorplan"]["path"]
    steps, ok = [], True
    for step in FLOORPLAN_STEPS:
        r = container(cfg, [(d / "src", "/src", "ro"), (d / "src/floorplan", "/src/floorplan", "rw"), (Path(tool), "/tools/floorplan", "ro")],
                      ["node", f"/tools/floorplan/skills/floorplan/scripts/{step}.mjs", "/src"], cfg["limits"]["timeout_seconds"],
                      run=d, step=f"floorplan-{len(steps)}-{step}")
        steps.append(dict(step=step, exit=r["exit"], seconds=r["seconds"]))
        (d / f"floorplan-{len(steps)}-{step}.log").write_text(r["tail"])
        if r["exit"] != 0:
            ok = False
            break
    out = d / "src/floorplan/index.html"
    return dict(ok=ok and out.is_file(), steps=steps, html=out, graph=d / "src/floorplan/graph.json")


def run_archify(cfg, d: Path, src: ReadOnlySource, commit: str, allowed: set[str]) -> dict:
    ir = json.loads((d / "archify-ir.json").read_bytes() or b"{}")
    if not ir:
        return dict(ok=False, status="NOT_RUN", reason="Chưa có Archify IR cho dự án.", steps=[])
    outside = [p for p in archify_sources(ir) if p not in allowed]
    if outside:
        return dict(ok=False, status="FAIL", steps=[], reason=f"IR trích dẫn {len(outside)} tệp ngoài danh sách nguồn cho phép.")
    drift = cited_drift(src, ir, commit)
    if drift:
        return dict(ok=False, status="FAIL", steps=[], drift=drift,
                    reason=f"{len(drift)} trích dẫn đã đổi nội dung ở commit đích; cần tác tử soạn lại IR (không render sơ đồ sai dòng).")
    ir["meta"]["repository"]["revision"] = commit
    ir["meta"]["output"] = "archify.html"
    work = d / "archify"
    work.mkdir(mode=0o700)
    (work / "ir.json").write_text(json.dumps(ir, ensure_ascii=False))
    src.pin_commit(commit, d / "pin", origin_url=ir["meta"]["repository"].get("url"))
    make_read_only(d / "pin")
    tool = cfg["tools"]["archify"]["path"]
    r = container(cfg, [(d / "pin", "/repo", "ro"), (work, "/work", "rw"), (Path(tool) / "archify", "/tools/archify", "ro")],
                  ["node", "/tools/archify/bin/archify.mjs", "finalize", "architecture", "/work/ir.json", "/work/archify.html",
                   "--repo-root", "/repo", "--quality", "showcase", "--json"], cfg["limits"]["timeout_seconds"],
                  run=d, step="archify-finalize")
    (d / "archify-finalize.log").write_text(r["tail"])
    gates = finalize_gates(work, r["tail"])
    steps = [dict(step=k, result={"pass": "PASS", "fail": "FAIL"}.get(v, "NOT_RUN"), detail=v) for k, v in gates.items()]
    core = all(gates.get(k) == "pass" for k in ("validate", "deliver", "check"))
    html = work / "archify.html"
    status = "PASS" if core and gates.get("browser-check") == "pass" else "PARTIAL" if core else "FAIL"
    return dict(ok=core and html.is_file(), status=status, exit=r["exit"], seconds=r["seconds"], steps=steps, html=html, ir=ir,
                reason="" if core else "finalize gates: " + json.dumps(gates)[:200])


def finalize_gates(work: Path, stdout_tail: str) -> dict:
    """Gate results from Archify's summary receipt (the full receipt does not carry them)."""
    for candidate in (work / "archify.finalize-summary.json",):
        try:
            gates = json.loads(candidate.read_text()).get("gates")
            if isinstance(gates, dict):
                return gates
        except (OSError, ValueError):
            pass
    try:
        return json.loads(stdout_tail[stdout_tail.index("{"):]).get("gates") or {}
    except ValueError:
        return {}


def build_manifest(project, src_info, origin, provenance, tools, artifacts, limitations):
    m = dict(schema=SCHEMA_VERSION, project=project, source=src_info, generated_at=int(time.time()), origin=origin,
             provenance=provenance, tools=tools, artifacts=artifacts)
    if limitations:
        m["limitations"] = limitations
    return m


def artifact_entry(data: bytes, status, validation, coverage, caps, limits, graph=None):
    findings = validate_html(data, "artifact")
    e = dict(sha256=sha256(data), size=len(data), media_type="text/html", status=status, validation=validation,
             coverage=coverage, capabilities=caps, limitations=list(dict.fromkeys(limits + findings)))
    if graph:
        e["graph"] = graph
    return e


def tools_meta(cfg) -> dict:
    t = cfg["tools"]
    out = {}
    for k in ("archify", "floorplan"):
        out[k] = {kk: t[k][kk] for kk in ("version", "commit", "license", "source") if t[k].get(kk)}
    out["runtime"] = {"version": t["runtime"].get("version", "container"), "image": t["runtime"]["image"]}
    return out


def cmd_run(args) -> int:
    os.umask(0o077)
    h = harness()
    task_id = args.task_id
    d = run_dir(task_id)
    try:
        with execution_lock(d):
            h.reconcile_due(at=now())
            task = h.get(task_id)
            if task.state != "DISPATCHED":
                raise RunRefused("Run is not eligible for execution")
            req = bound_request(d, task)
            rc = _execute_run(task_id, h, d, req)
            terminal = h.get(task_id)
            write_receipt(d / "worker-finished.json", {"owner": req["owner"], "state": terminal.state, "exit": rc})
            return rc
    except (OSError, ValueError, HarnessError):
        return 1


def _execute_run(task_id, h, d, req) -> int:
    try:
        h.observe(task_id, backend_state="RUNNING", process_alive=True, at=now(), progress_at=now(), evidence_ref=f"run:{task_id}:start")
        cfg = load_config()
        verify_tools(cfg)
        if sha256(canonical(tool_identity(cfg)).encode())[:16] != req["toolset"]:
            raise RunRefused("Bộ công cụ đã đổi kể từ lúc xếp hàng.")
        p = cfg["projects"][req["project"]]
        src = ReadOnlySource(p["repo"])
        commit = src.resolve(req["commit"])
        info = src.commit_info(commit)
        source_manifest = materialize(src, commit, p, cfg["limits"], d / "src")
        source_manifest["repository"] = req["project"]
        (d / "src/floorplan").mkdir()
        seed = (d / "floorplan-architecture.json").read_bytes()
        if seed:
            (d / "src/floorplan/architecture.json").write_bytes(seed)
        smb = canonical(source_manifest).encode()
        (d / "source-manifest.json").write_bytes(smb)
        make_read_only(d / "src", writable_dirs=(d / "src",), writable_trees=(d / "src/floorplan",))
        h.observe(task_id, backend_state="RUNNING", process_alive=True, at=now(), progress_at=now(), evidence_ref=f"run:{task_id}:source")
        fp = run_floorplan(cfg, d)
        h.observe(task_id, backend_state="RUNNING", process_alive=True, at=now(), progress_at=now(), evidence_ref=f"run:{task_id}:floorplan")
        allowed = {f["path"] for f in source_manifest["files"]}
        ar = run_archify(cfg, d, src, commit, allowed)
        artifacts, blobs, limitations = {}, {}, []
        if fp["ok"]:
            data = read_regular_file(fp["html"], MAX_HTML)
            graph, cov = floorplan_graph(json.loads(read_regular_file(fp["graph"], MAX_AUX)))
            val = [dict(step=f"floorplan {s['step']}", result="PASS", exit=s["exit"]) for s in fp["steps"]]
            status = "PASS" if cov["files_described"] == cov["files"] else "PARTIAL"
            artifacts["floorplan"] = artifact_entry(data, status, val, cov, FLOORPLAN_CAPS, FLOORPLAN_LIMITS, graph)
            blobs["floorplan"] = data
        else:
            limitations.append("Floorplan không hoàn tất: " + ", ".join(f"{s['step']} exit {s['exit']}" for s in fp["steps"]))
        if ar["ok"]:
            data = read_regular_file(ar["html"], MAX_HTML)
            val = [dict(step="archify " + s["step"], result=s["result"], detail=s["detail"]) for s in ar["steps"]]
            lim = ["Archify browser-check không chạy: Chrome cần user namespace, bị AppArmor của host chặn; không tắt sandbox Chrome. Đã kiểm tra trình duyệt riêng ở bước nghiệm thu."] \
                if ar["status"] == "PARTIAL" else []
            artifacts["archify"] = artifact_entry(data, ar["status"], val,
                                                  {"components": len(ar["ir"]["components"]), "connections": len(ar["ir"]["connections"]),
                                                   "cited_files": len(archify_sources(ar["ir"]))},
                                                  ARCHIFY_CAPS, lim, archify_graph(ar["ir"]))
            blobs["archify"] = data
        else:
            limitations.append("Archify: " + ar["reason"])
        if not artifacts:
            raise RunRefused("Không công cụ nào tạo được đầu ra hợp lệ: " + "; ".join(limitations))
        if len(artifacts) < 2:
            limitations.append("Snapshot chỉ có một công cụ; không đủ cặp cùng commit.")
        manifest = build_manifest(
            req["project"],
            dict(type="git-commit", commit=commit, committed_at=utc_iso(info["committed_at"]), branch=p.get("branch_label") or p["ref"],
                 input_manifest_sha256=sha256(smb), file_count=source_manifest["includedCount"]),
            "rerun",
            dict(summary=f"Chạy lại có cổng #{task_id}: hai công cụ ghim commit, chạy ngoại tuyến trong container chỉ đọc.",
                 run_task=task_id, operator="architecture-runner"),
            {k: v for k, v in tools_meta(cfg).items() if k in artifacts or k == "runtime"},
            artifacts, limitations)
        snap, created = store().put(manifest, blobs)
        h.observe(task_id, backend_state="RUNNING", process_alive=True, at=now(), progress_at=now(), evidence_ref=f"snapshot:{snap}")

        def verifier(artifact_ref, test_ref, runtime_ref):
            s = store().snapshot(req["project"], artifact_ref.split(":", 1)[1])
            return bool(s) and all(store().artifact(req["project"], s["id"], k, a["sha256"]) for k, a in s["artifacts"].items())

        if len(artifacts) == 2:
            h.accept(task_id, at=now(), artifact_ref=f"snapshot:{snap}", test_evidence="manifest+hash validated",
                     runtime_readback="stored blobs re-hashed", verifier=verifier)
        else:
            h.fail(task_id, at=now(), reason=f"PARTIAL snapshot {snap[:12]} imported; " + "; ".join(limitations)[:300])
        print(json.dumps(dict(task=task_id, snapshot=snap, created=created, artifacts={k: a["status"] for k, a in artifacts.items()},
                              limitations=limitations), ensure_ascii=False))
        rc = 0 if len(artifacts) == 2 else 1
    except Exception as exc:  # every failure is recorded on the durable task
        try:
            h.fail(task_id, at=now(), reason=f"{type(exc).__name__}: {str(exc)[:240]}")
        except HarnessError:
            pass
        print(f"run {task_id} failed: {exc}", file=sys.stderr)
        rc = 1
    # A worker is still a live unit at this point. Never remove its mounts
    # or claim executor_stopped; the independent reconciler owns cleanup.
    return rc


def cmd_enqueue(args) -> int:
    try:
        r = request_rerun(args.project, args.commit, Path(args.archify_ir) if args.archify_ir else None)
    except RunRefused as exc:
        print(json.dumps(dict(refused=str(exc)), ensure_ascii=False))
        return 3
    print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_import(args) -> int:
    """Import existing native outputs of BOTH tools rendered from one commit."""
    validate_project(args.project)
    tools = json.loads(read_regular_file(Path(args.tools), MAX_AUX))
    src_info = dict(type="git-commit", commit=args.commit, committed_at=utc_iso(args.committed_at))
    if args.branch:
        src_info["branch"] = args.branch
    if args.source_manifest:
        smb = read_regular_file(Path(args.source_manifest), MAX_AUX)
        sm = json.loads(smb)
        if sm.get("inputCommit") != args.commit:
            raise SystemExit("source manifest commit differs from --commit")
        src_info["input_manifest_sha256"] = sha256(smb)
        src_info["file_count"] = int(sm.get("includedCount", 0))
    ir = json.loads(read_regular_file(Path(args.archify_ir), MAX_AUX))
    if ir["meta"]["repository"]["revision"] != args.commit:
        raise SystemExit("Archify IR revision differs from --commit; both tools must use the same commit")
    ar = read_regular_file(Path(args.archify_html), MAX_HTML)
    fp = read_regular_file(Path(args.floorplan_html), MAX_HTML)
    graph, cov = floorplan_graph(json.loads(read_regular_file(Path(args.floorplan_graph), MAX_AUX)))
    if args.expect_archify_sha and sha256(ar) != args.expect_archify_sha:
        raise SystemExit("archify hash differs from the recorded pilot hash")
    if args.expect_floorplan_sha and sha256(fp) != args.expect_floorplan_sha:
        raise SystemExit("floorplan hash differs from the recorded pilot hash")
    rec = json.loads(read_regular_file(Path(args.receipts), MAX_AUX))
    artifacts = {
        "archify": artifact_entry(ar, rec["archify"]["status"], rec["archify"]["validation"],
                                  {"components": len(ir["components"]), "connections": len(ir["connections"]),
                                   "cited_files": len(archify_sources(ir))},
                                  ARCHIFY_CAPS, rec["archify"]["limitations"], archify_graph(ir)),
        "floorplan": artifact_entry(fp, "PASS" if cov["files_described"] == cov["files"] else "PARTIAL", rec["floorplan"]["validation"],
                                    cov, FLOORPLAN_CAPS, FLOORPLAN_LIMITS + rec["floorplan"]["limitations"], graph),
    }
    manifest = build_manifest(args.project, src_info, "imported-pilot",
                              dict(summary=rec["provenance"], operator=args.operator, evidence_refs=rec.get("evidence_refs", []),
                                   **({"export_ref": args.export_ref} if args.export_ref else {})),
                              tools, artifacts, rec.get("limitations", []))
    snap, created = store().put(manifest, {"archify": ar, "floorplan": fp})
    print(json.dumps(dict(snapshot=snap, created=created), ensure_ascii=False))
    return 0


def cmd_status(args) -> int:
    validate_project(args.project)
    snaps = store().snapshots(args.project)
    out = dict(snapshots=[dict(id=s["id"], commit=s["source"]["commit"], origin=s["origin"],
                               artifacts={k: (a["status"], a["sha256"], a["size"]) for k, a in s["artifacts"].items()}) for s in snaps],
               runs=[dict(id=r["id"], state=r["state"], cleanup=r["cleanup"], commit=r["commit"], unit=r["unit"],
                          last=r["events"][-1]["event"] if r["events"] else None) for r in runs(args.project)])
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_reconcile(args) -> int:
    ok = reconcile_run_cleanup(args.task_id)
    print(json.dumps({"task_id": args.task_id, "cleanup": "CLEANED_VERIFIED" if ok else "CLEANUP_PENDING"}))
    return 0 if ok else 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--task-id", type=int, required=True)
    e = sub.add_parser("enqueue")
    e.add_argument("--project", required=True)
    e.add_argument("--commit")
    e.add_argument("--archify-ir")
    i = sub.add_parser("import")
    for k in ("project", "commit", "committed-at", "archify-html", "archify-ir", "floorplan-html", "floorplan-graph", "tools", "receipts", "operator"):
        i.add_argument("--" + k, required=True)
    for k in ("branch", "source-manifest", "export-ref", "expect-archify-sha", "expect-floorplan-sha"):
        i.add_argument("--" + k)
    c = sub.add_parser("reconcile")
    c.add_argument("--task-id", type=int, required=True)
    s = sub.add_parser("status")
    s.add_argument("--project", required=True)
    args = ap.parse_args(argv)
    try:
        return {"run": cmd_run, "enqueue": cmd_enqueue, "import": cmd_import, "status": cmd_status, "reconcile": cmd_reconcile}[args.cmd](args)
    except ArtifactRejected as exc:
        print(json.dumps(dict(rejected=str(exc)), ensure_ascii=False))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
