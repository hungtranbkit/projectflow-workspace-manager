"""End-to-end visual QA stage through TestRunner and real local subprocesses.

This uses a disposable git worktree-like fixture directory, the real SQLite
Database, TestRunner's default direct-host SandboxedCommandRunner, a loopback
fixture server, and the pinned UIFix CLI. No project/provider/production data
is involved. Evidence is copied only when VISUAL_QA_RUNNER_EVIDENCE_DIR is set.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from app.db import Database
from app.services.project_contract import ContractError
from app.services.task_decision_service import TaskDecisionService
from app.services.test_runner import TestRunner as _ProjectFlowTestRunner


class _Git:
    def head(self, path: Path) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path,
            check=True, text=True, capture_output=True,
        )
        return result.stdout.strip()


class _FixtureServer:
    def __init__(self):
        self.methods: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner_self):
                self.methods.append(("GET", inner_self.path))
                color = "#18395a" if inner_self.path == "/same" else "#a53179"
                page = (
                    "<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>"
                    f"<style>*{{box-sizing:border-box}}html,body{{margin:0;width:100%;height:100%;background:{color}}}</style>"
                    "</head><body></body></html>"
                )
                body = page.encode()
                inner_self.send_response(200)
                inner_self.send_header("content-type", "text/html; charset=utf-8")
                inner_self.send_header("content-length", str(len(body)))
                inner_self.end_headers()
                inner_self.wfile.write(body)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _write_reference(root: Path) -> Path:
    reference = root / "docs" / "reference.png"
    reference.parent.mkdir(parents=True)
    uifix_package = Path(__file__).resolve().parents[1] / ".runtime" / "uifix-v1.2.6" / "package.json"
    script = (
        "const {createRequire}=require('node:module');"
        f"const sharp=createRequire({json.dumps(str(uifix_package))})('sharp');"
        f"sharp({{create:{{width:320,height:240,channels:3,background:{{r:24,g:57,b:90}}}}}})"
        f".png().toFile({json.dumps(str(reference))}).catch(e=>{{console.error(e);process.exitCode=1}});"
    )
    subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    return reference


def _write_project(root: Path, *, url: str, command: str, reference: str = "docs/reference.png") -> None:
    config = {
        "commands": {
            "test": {"command": "python3 -c 'print(\"fixture-test-command-ran\")'"},
            "visual_qa": {"command": command},
        },
        "ci": {"required": ["test"]},
        "visual_qa": {
            "enabled": True,
            "reference": reference,
            "route": url,
            "viewports": [{"width": 320, "height": 240}],
            "threshold": 0,
        },
    }
    (root / "PROJECT.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _commit(root: Path, *paths: str, message: str) -> str:
    subprocess.run(["git", "add", *paths], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True)
    return _Git().head(root)


def _command(root: Path, *, url: str, out: str, viewport: str = "320x240") -> str:
    cli = Path(__file__).resolve().parents[1] / "tools" / "visual_qa" / "visual-qa.mjs"
    return shlex.join([
        "node", str(cli), "--reference", "docs/reference.png", "--url", url,
        "--viewport", viewport, "--out", out, "--threshold", "0",
    ])


def _add_task_and_workspace(
    db: Database, root: Path, prompt: str, suffix: str,
    repo_id: int | None = None, worktree_path: Path | None = None,
) -> tuple[int, int]:
    if repo_id is None:
        repo_id = db.execute(
            "INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)",
            (f"fixture-{suffix}", str(root)),
        )
    task_id = db.execute(
        "INSERT INTO tasks(slug,title,status,implementation_prompt) VALUES(?,?,?,?)",
        (f"visual-{suffix}", f"Visual QA {suffix}", "ACTIVE", prompt),
    )
    workspace_id = db.execute(
        "INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,task_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (repo_id, "codex", f"visual-{suffix}", f"test/{suffix}", str(worktree_path or root), "main", _Git().head(worktree_path or root), task_id),
    )
    return task_id, workspace_id


def _run_stages(runner: _ProjectFlowTestRunner, db: Database, workspace_id: int, root: Path) -> list[dict]:
    run_ids = runner.start("agent", workspace_id, root)
    deadline = time.monotonic() + 50
    rows = []
    while time.monotonic() < deadline:
        rows = db.all(
            "SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=? ORDER BY id",
            (workspace_id,),
        )
        if rows and all(row["status"] not in {"QUEUED", "RUNNING"} for row in rows):
            break
        time.sleep(0.05)
    assert rows and all(row["status"] not in {"QUEUED", "RUNNING"} for row in rows), "real TestRunner subprocess stages timed out"
    assert [row["id"] for row in rows[-len(run_ids):]] == run_ids
    return rows[-len(run_ids):]


def _decision(db: Database, git: _Git, workspace_id: int, task_id: int, root: Path, head: str) -> str:
    return TaskDecisionService(db, git).builder_tests_status({
        "id": workspace_id, "task_id": task_id, "head": head, "worktree_path": str(root),
    })


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _preserve_evidence(evidence_dir: Path, root: Path, reference: Path, captures: dict[str, Path], details: dict) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(reference, evidence_dir / "reference.png")
    capture_summaries = {}
    for label, output in captures.items():
        summary = {}
        for name in ("actual.png", "diff.png", "overlay.png"):
            source = output / name
            if source.is_file():
                target_name = f"{label}-{name}"
                shutil.copyfile(source, evidence_dir / target_name)
                summary[name] = {"file": target_name, "sha256": _sha(source)}
        metrics_path = output / "metrics.json"
        if metrics_path.is_file():
            metrics = json.loads(metrics_path.read_text())
            safe_metrics = {
                key: metrics[key]
                for key in (
                    "schemaVersion", "tool", "toolVersion", "toolCommit", "sourceCommit",
                    "referenceSha256", "actualSha256", "viewport", "captureMode",
                    "imageDimensions", "deviceScaleFactor", "threshold", "percentDiff",
                    "mismatchedPixels", "totalPixels", "ssim", "horizontalOverflow",
                    "pageErrors", "blockedRequests", "passed",
                )
                if key in metrics
            }
            (evidence_dir / f"{label}-metrics.json").write_text(json.dumps(safe_metrics, indent=2) + "\n")
            summary["metrics"] = safe_metrics
        capture_summaries[label] = summary
    result = {**details, "referenceSha256": _sha(reference), "captures": capture_summaries}
    (evidence_dir / "runner-results.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = [
        "# ProjectFlow visual QA subprocess integration",
        "",
        "Fixture-only end-to-end test: real TestRunner background dispatch, real SQLite, real host subprocess runner, loopback HTML server, pinned UIFix CLI. No production project or provider was used.",
        "",
        f"- Source HEAD for passing visual capture: `{details['positiveHead']}`",
        f"- Reference SHA-256: `{_sha(reference)}` (fixture image, 320x240)",
        "- ProjectFlow policy threshold: 0 for exact synthetic-fixture integration; this is not the product 8% pilot threshold.",
        "- Replay test: `python3 -m pytest -q tests/test_visual_qa_runner_subprocess.py` (set `CHROME_PATH` and `VISUAL_QA_PLAYWRIGHT_FROM` for the authorized local browser when needed).",
        "- Commands run by `TestRunner`: local `python3 -c ...` test stage and pinned `node tools/visual_qa/visual-qa.mjs` visual stage.",
        "- Verified: pass, color-difference fail, dimension fail-closed, unavailable-server fail-closed, missing-reference contract rejection, backend opt-out, and stale commit evidence returns `NOT_RUN`.",
        "",
        "Artifacts in this directory include reference, actual, diff, overlay, sanitized UIFix metrics and stage results. The reference is generated for this test only and is not a product design approval.",
        "",
    ]
    (evidence_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    manifest = {}
    for path in sorted(evidence_dir.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS.json":
            manifest[path.name] = _sha(path)
    (evidence_dir / "SHA256SUMS.json").write_text(json.dumps(manifest, indent=2) + "\n")


def test_visual_gate_runs_through_real_test_runner_subprocess_and_pins_current_head(tmp_path):
    root = tmp_path / "fixture-project"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "qa@example.invalid"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Visual QA Test"], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("fixture only\n")
    reference = _write_reference(root)

    fixture = _FixtureServer()
    db = Database(tmp_path / "projectflow.sqlite")
    db.init()
    git = _Git()
    positive_out = root / ".artifacts" / "positive"
    _write_project(root, url=f"{fixture.base_url}/same", command=_command(root, url=f"{fixture.base_url}/same", out=".artifacts/positive"))
    positive_head = _commit(root, "README.md", "docs/reference.png", "PROJECT.yaml", message="fixture positive visual contract")
    repo_id = db.execute(
        "INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("fixture-ui", str(root))
    )
    task_id, workspace_id = _add_task_and_workspace(db, root, "UI task\nVISUAL_QA_REQUIRED: true", "ui", repo_id)
    runner = _ProjectFlowTestRunner(db, git)  # real default SandboxedCommandRunner; mandatory=False on local test fixture
    captures: dict[str, Path] = {}
    try:
        positive_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in positive_rows] == [
            ("test", "PASS", 0), ("visual_qa", "PASS", 0),
        ]
        assert {r["tested_commit"] for r in positive_rows} == {positive_head}
        assert _decision(db, git, workspace_id, task_id, root, positive_head) == "PASS"
        captures["positive"] = positive_out

        # A source commit after passing invalidates the old run by exact SHA.
        (root / "revision.txt").write_text("next source revision\n")
        stale_head = _commit(root, "revision.txt", message="new source revision")
        assert stale_head != positive_head
        assert _decision(db, git, workspace_id, task_id, root, stale_head) == "NOT_RUN"

        # A real color deviation goes through TestRunner's subprocess and fails UIFix.
        negative_out = root / ".artifacts" / "negative-color"
        _write_project(root, url=f"{fixture.base_url}/wrong", command=_command(root, url=f"{fixture.base_url}/wrong", out=".artifacts/negative-color"))
        negative_head = _commit(root, "PROJECT.yaml", message="fixture negative color contract")
        negative_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in negative_rows] == [
            ("test", "PASS", 0), ("visual_qa", "FAIL", 1),
        ]
        assert _decision(db, git, workspace_id, task_id, root, negative_head) == "FAIL"
        captures["negative-color"] = negative_out

        # The runner preserves the CLI's dimensions failure exit (2) as a gate FAIL.
        dimension_out = root / ".artifacts" / "dimension-mismatch"
        _write_project(root, url=f"{fixture.base_url}/same", command=_command(root, url=f"{fixture.base_url}/same", out=".artifacts/dimension-mismatch", viewport="321x240"))
        dimension_head = _commit(root, "PROJECT.yaml", message="fixture invalid dimensions")
        dimension_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in dimension_rows] == [
            ("test", "PASS", 0), ("visual_qa", "FAIL", 2),
        ]
        assert any("REFERENCE_DIMENSIONS_MISMATCH" in (r["stderr_tail"] or "") for r in dimension_rows)
        assert _decision(db, git, workspace_id, task_id, root, dimension_head) == "FAIL"

        # Invalid reference config fails before any stage row/subprocess is created.
        missing_rows_before = len(db.all("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=?", (workspace_id,)))
        _write_project(root, url=f"{fixture.base_url}/same", command=_command(root, url=f"{fixture.base_url}/same", out=".artifacts/missing-reference"), reference="docs/missing.png")
        missing_head = _commit(root, "PROJECT.yaml", message="fixture missing reference")
        try:
            runner.start("agent", workspace_id, root)
            raise AssertionError("missing reference should fail closed before dispatch")
        except ContractError as exc:
            missing_reference_error = str(exc)
        assert "does not exist" in missing_reference_error
        assert len(db.all("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=?", (workspace_id,))) == missing_rows_before
        # No current-head run exists because validation stopped before dispatch;
        # TaskDecisionService must not turn absence of evidence into PASS.
        assert _decision(db, git, workspace_id, task_id, root, missing_head) == "NOT_RUN"

        # A syntactically valid loopback URL with no server fails in the real CLI process.
        fixture.close()
        unavailable_out = root / ".artifacts" / "unavailable-server"
        _write_project(root, url=f"{fixture.base_url}/same", command=_command(root, url=f"{fixture.base_url}/same", out=".artifacts/unavailable-server"))
        unavailable_head = _commit(root, "PROJECT.yaml", message="fixture unavailable server")
        unavailable_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in unavailable_rows] == [
            ("test", "PASS", 0), ("visual_qa", "FAIL", 2),
        ]
        assert any("SERVER_NOT_READY" in (r["stderr_tail"] or "") for r in unavailable_rows)
        assert _decision(db, git, workspace_id, task_id, root, unavailable_head) == "FAIL"

        # Unmarked backend tasks only run the ordinary test command, even with malformed visual config.
        backend_root = tmp_path / "backend-worktree"
        subprocess.run(["git", "clone", "--quiet", str(root), str(backend_root)], check=True, text=True, capture_output=True)
        backend_id, backend_workspace_id = _add_task_and_workspace(
            db, root, "Backend-only API change", "backend", repo_id, backend_root
        )
        backend_rows = _run_stages(runner, db, backend_workspace_id, backend_root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in backend_rows] == [("test", "PASS", 0)]
        assert _decision(db, git, backend_workspace_id, backend_id, backend_root, unavailable_head) == "PASS"

        assert fixture.methods and all(method == "GET" for method, _path in fixture.methods)
        assert {path for _method, path in fixture.methods} <= {"/same", "/wrong"}

        evidence = os.environ.get("VISUAL_QA_RUNNER_EVIDENCE_DIR")
        if evidence:
            repo_root = Path(__file__).resolve().parents[1]
            evidence_dir = (repo_root / evidence).resolve()
            assert evidence_dir.is_relative_to(repo_root), "evidence directory must stay inside repository"
            _preserve_evidence(evidence_dir, root, reference, captures, {
                "positiveHead": positive_head,
                "negativeHead": negative_head,
                "dimensionMismatchHead": dimension_head,
                "missingReferenceHead": missing_head,
                "unavailableServerHead": unavailable_head,
                "backendHead": unavailable_head,
                "testStage": "PASS / exit 0 (real subprocess)",
                "positiveVisualStage": "PASS / exit 0 (real pinned UIFix CLI subprocess)",
                "negativeVisualStage": "FAIL / exit 1 (real color deviation)",
                "dimensionVisualStage": "FAIL / exit 2 (reference dimensions rejected)",
                "missingReference": f"TestRunner contract rejected before dispatch: {missing_reference_error}",
                "unavailableServer": "FAIL / exit 2 (real CLI SERVER_NOT_READY)",
                "backendOptOut": "PASS / only test stage dispatched",
                "staleEvidence": "NOT_RUN after source HEAD changed",
                "externalRequests": [],
                "httpMethods": sorted(set(method for method, _path in fixture.methods)),
            })
    finally:
        fixture.close()
