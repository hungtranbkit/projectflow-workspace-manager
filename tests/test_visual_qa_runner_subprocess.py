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
from app.services.git_workspace import GitWorkspaceService
from app.services.sandbox_runtime import SandboxRuntimeService
from app.services.sandboxed_exec import SandboxedCommandRunner
from app.services.task_decision_service import TaskDecisionService
from app.services.test_runner import TestRunner as _ProjectFlowTestRunner


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


class _ArtifactRecordingRunner:
    """Observes artifacts while delegating every command to the real runner."""
    mandatory = False

    def __init__(self, tmp_path: Path):
        self.inner = SandboxedCommandRunner(SandboxRuntimeService(), mandatory=False)
        self.captured: dict[int, Path] = {}
        self.capture_root = tmp_path / "subprocess-artifact-captures"

    def run(self, command, worktree_root, working_dir, timeout, env=None):
        result = self.inner.run(command, worktree_root, working_dir, timeout, env=env)
        run_id = (env or {}).get("VISUAL_QA_RUN_ID")
        if run_id:
            source = Path(worktree_root).absolute() / ".runtime" / "projectflow-visual-qa" / str(run_id)
            if source.is_dir() and any(source.iterdir()):
                destination = self.capture_root / str(run_id)
                shutil.copytree(source, destination)
                self.captured[int(run_id)] = destination
        return result


def _write_reference(root: Path) -> Path:
    reference = root / "docs" / "reference.png"
    reference.parent.mkdir(parents=True)
    uifix_package = root / ".runtime" / "uifix-v1.2.6" / "package.json"
    script = (
        "const {createRequire}=require('node:module');"
        f"const sharp=createRequire({json.dumps(str(uifix_package))})('sharp');"
        f"sharp({{create:{{width:320,height:240,channels:3,background:{{r:24,g:57,b:90}}}}}})"
        f".png().toFile({json.dumps(str(reference))}).catch(e=>{{console.error(e);process.exitCode=1}});"
    )
    subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    return reference


def _install_visual_cli_fixture(root: Path) -> None:
    """Model a managed project with its local CLI and pinned runtime path."""
    project_runtime = root / ".runtime"
    project_runtime.mkdir(parents=True, exist_ok=True)
    pinned_runtime = Path(__file__).resolve().parents[1] / ".runtime" / "uifix-v1.2.6"
    (project_runtime / "uifix-v1.2.6").symlink_to(pinned_runtime, target_is_directory=True)
    cli_dir = root / "tools" / "visual_qa"
    cli_dir.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "tools" / "visual_qa" / "visual-qa.mjs",
        cli_dir / "visual-qa.mjs",
    )


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


def _commit(root: Path, git: GitWorkspaceService, *paths: str, message: str) -> str:
    subprocess.run(["git", "add", *paths], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True)
    return git.head(root)


def _command(root: Path, *, url: str, viewport: str = "320x240") -> str:
    cli = root / "tools" / "visual_qa" / "visual-qa.mjs"
    return shlex.join([
        "node", str(cli), "--reference", "docs/reference.png", "--url", url,
        "--viewport", viewport, "--out", ".runtime/standalone-output-is-overridden-by-TestRunner",
        "--threshold", "0",
    ])


def _add_task_and_workspace(
    db: Database, git: GitWorkspaceService, root: Path, prompt: str, suffix: str,
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
        (repo_id, "codex", f"visual-{suffix}", f"test/{suffix}", str(worktree_path or root), "main", git.head(worktree_path or root), task_id),
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


def _decision(db: Database, git: GitWorkspaceService, workspace_id: int, task_id: int, root: Path, head: str) -> str:
    evidence_root = root.parent / "coordinator-state" / "visual-qa-evidence"
    return TaskDecisionService(db, git, visual_evidence_root=evidence_root).builder_tests_status({
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
                    "referenceSha256", "actualSha256", "url", "viewport", "captureMode",
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
        "Fixture-only end-to-end test: real TestRunner background dispatch, real SQLite, real SandboxedCommandRunner direct-host mode inside an isolated container, loopback HTML server, pinned UIFix CLI. An observing decorator copies CLI output before TestRunner cleanup but delegates execution to the real runner. No production project or provider was used.",
        "",
        f"- ProjectFlow PR source: `{details['projectFlowSourceCommit']}` / tree `{details['projectFlowSourceTree']}`",
        f"- Fixture source SHA for positive capture: `{details['positiveHead']}`",
        f"- Exact PR head tested: `{details['publishedPrHead']}`",
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
    (root / ".gitignore").write_text(".runtime/\n.artifacts/\n", encoding="utf-8")
    _install_visual_cli_fixture(root)
    reference = _write_reference(root)

    fixture = _FixtureServer()
    initial_fixture_methods = fixture.methods
    initial_base_url = fixture.base_url
    db = Database(tmp_path / "projectflow.sqlite")
    db.init()
    git = GitWorkspaceService(tmp_path)
    _write_project(root, url=f"{fixture.base_url}/same", command=_command(root, url=f"{fixture.base_url}/same"))
    positive_head = _commit(root, git, "README.md", ".gitignore", "tools/visual_qa/visual-qa.mjs", "docs/reference.png", "PROJECT.yaml", message="fixture positive visual contract")
    repo_id = db.execute(
        "INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("fixture-ui", str(root))
    )
    task_id, workspace_id = _add_task_and_workspace(db, git, root, "UI task\nVISUAL_QA_REQUIRED: true", "ui", repo_id)
    evidence_root = tmp_path / "coordinator-state" / "visual-qa-evidence"
    observing_runner = _ArtifactRecordingRunner(tmp_path)
    runner = _ProjectFlowTestRunner(db, git, runner=observing_runner, visual_evidence_root=evidence_root)
    captures: dict[str, Path] = {}
    decisions: dict[str, str] = {}
    try:
        positive_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in positive_rows] == [
            ("test", "PASS", 0), ("visual_qa", "PASS", 0),
        ]
        assert {r["tested_commit"] for r in positive_rows} == {positive_head}
        visual_row = next(row for row in positive_rows if row["stage"] == "visual_qa")
        decisions["positive"] = _decision(db, git, workspace_id, task_id, root, positive_head)
        assert decisions["positive"] == "PASS"
        captures["positive"] = observing_runner.captured[int(visual_row["id"])] / "320x240"

        # A source commit after passing invalidates the old run by exact SHA.
        (root / "revision.txt").write_text("next source revision\n")
        stale_head = _commit(root, git, "revision.txt", message="new source revision")
        assert stale_head != positive_head
        decisions["staleAfterSourceChange"] = _decision(db, git, workspace_id, task_id, root, stale_head)
        assert decisions["staleAfterSourceChange"] == "NOT_RUN"

        # A real color deviation goes through TestRunner's subprocess and fails UIFix.
        _write_project(root, url=f"{fixture.base_url}/wrong", command=_command(root, url=f"{fixture.base_url}/wrong"))
        negative_head = _commit(root, git, "PROJECT.yaml", message="fixture negative color contract")
        negative_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in negative_rows] == [
            ("test", "PASS", 0), ("visual_qa", "FAIL", 1),
        ]
        decisions["negativeColor"] = _decision(db, git, workspace_id, task_id, root, negative_head)
        assert decisions["negativeColor"] == "FAIL"
        negative_visual_row = next(row for row in negative_rows if row["stage"] == "visual_qa")
        captures["negative-color"] = observing_runner.captured[int(negative_visual_row["id"])] / "320x240"

        # The runner preserves the CLI's dimensions failure exit (2) as a gate FAIL.
        _write_project(root, url=f"{fixture.base_url}/same", command=_command(root, url=f"{fixture.base_url}/same", viewport="321x240"))
        dimension_head = _commit(root, git, "PROJECT.yaml", message="fixture invalid dimensions")
        dimension_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in dimension_rows] == [
            ("test", "PASS", 0), ("visual_qa", "FAIL", 2),
        ]
        assert any("REFERENCE_DIMENSIONS_MISMATCH" in (r["stderr_tail"] or "") for r in dimension_rows)
        decisions["dimensionMismatch"] = _decision(db, git, workspace_id, task_id, root, dimension_head)
        assert decisions["dimensionMismatch"] == "FAIL"

        # Invalid reference config fails before any stage row/subprocess is created.
        missing_rows_before = len(db.all("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=?", (workspace_id,)))
        _write_project(root, url=f"{fixture.base_url}/same", command=_command(root, url=f"{fixture.base_url}/same"), reference="docs/missing.png")
        missing_head = _commit(root, git, "PROJECT.yaml", message="fixture missing reference")
        try:
            runner.start("agent", workspace_id, root)
            raise AssertionError("missing reference should fail closed before dispatch")
        except ContractError as exc:
            missing_reference_error = str(exc)
        assert "does not exist" in missing_reference_error
        assert len(db.all("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=?", (workspace_id,))) == missing_rows_before
        # No current-head run exists because validation stopped before dispatch;
        # TaskDecisionService must not turn absence of evidence into PASS.
        decisions["missingReference"] = _decision(db, git, workspace_id, task_id, root, missing_head)
        assert decisions["missingReference"] == "NOT_RUN"

        # A syntactically valid loopback URL with no server fails in the real CLI process.
        fixture.close()
        _write_project(root, url=f"{fixture.base_url}/same", command=_command(root, url=f"{fixture.base_url}/same"))
        unavailable_head = _commit(root, git, "PROJECT.yaml", message="fixture unavailable server")
        unavailable_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in unavailable_rows] == [
            ("test", "PASS", 0), ("visual_qa", "FAIL", 2),
        ]
        assert any("SERVER_NOT_READY" in (r["stderr_tail"] or "") for r in unavailable_rows)
        decisions["unavailableServer"] = _decision(db, git, workspace_id, task_id, root, unavailable_head)
        assert decisions["unavailableServer"] == "FAIL"

        # Keep the original server-unavailable case, then use a fresh local
        # server for the later current-head tamper PASS.
        fixture = _FixtureServer()

        # Unmarked backend tasks only run the ordinary test command, even with malformed visual config.
        backend_root = tmp_path / "backend-worktree"
        subprocess.run(["git", "clone", "--quiet", str(root), str(backend_root)], check=True, text=True, capture_output=True)
        backend_id, backend_workspace_id = _add_task_and_workspace(
            db, git, root, "Backend-only API change", "backend", repo_id, backend_root
        )
        backend_rows = _run_stages(runner, db, backend_workspace_id, backend_root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in backend_rows] == [("test", "PASS", 0)]
        decisions["backendOptOut"] = _decision(db, git, backend_workspace_id, backend_id, backend_root, unavailable_head)
        assert decisions["backendOptOut"] == "PASS"

        # Exit code zero alone cannot produce visual evidence or gate PASS.
        _write_project(root, url=f"{fixture.base_url}/same", command="true")
        no_evidence_head = _commit(root, git, "PROJECT.yaml", message="exit zero without visual artifacts")
        no_evidence_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in no_evidence_rows] == [
            ("test", "PASS", 0), ("visual_qa", "FAIL", 0),
        ]
        assert "VISUAL_EVIDENCE_REJECTED" in (no_evidence_rows[-1]["stderr_tail"] or "")
        decisions["exitZeroWithoutEvidence"] = _decision(db, git, workspace_id, task_id, root, no_evidence_head)
        assert decisions["exitZeroWithoutEvidence"] == "FAIL"

        # A fresh PASS on the current source proves tamper detection is not
        # accidentally satisfied by the earlier stale-SHA result.
        _write_project(
            root, url=f"{fixture.base_url}/same",
            command=_command(root, url=f"{fixture.base_url}/same"),
        )
        tamper_current_head = _commit(root, git, "PROJECT.yaml", message="fresh positive visual evidence before tamper check")
        current_positive_rows = _run_stages(runner, db, workspace_id, root)
        assert [(r["stage"], r["status"], r["exit_code"]) for r in current_positive_rows] == [
            ("test", "PASS", 0), ("visual_qa", "PASS", 0),
        ]
        assert {r["tested_commit"] for r in current_positive_rows} == {tamper_current_head}
        decisions["tamperCurrentBeforeMutation"] = _decision(db, git, workspace_id, task_id, root, tamper_current_head)
        assert decisions["tamperCurrentBeforeMutation"] == "PASS"
        current_visual_row = next(row for row in current_positive_rows if row["stage"] == "visual_qa")
        captures["tamper-current-positive"] = observing_runner.captured[int(current_visual_row["id"])] / "320x240"
        current_evidence = evidence_root / "agent" / str(workspace_id) / str(current_visual_row["id"]) / "320x240" / "actual.png"
        current_evidence.write_bytes(b"tampered")
        decisions["tamperCurrentAfterMutation"] = _decision(db, git, workspace_id, task_id, root, tamper_current_head)
        assert decisions["tamperCurrentAfterMutation"] == "FAIL"

        all_methods = initial_fixture_methods + fixture.methods
        assert all_methods and all(method == "GET" for method, _path in all_methods)
        assert {path for _method, path in all_methods} <= {"/same", "/wrong"}

        evidence = os.environ.get("VISUAL_QA_RUNNER_EVIDENCE_DIR")
        if evidence:
            repo_root = Path(__file__).resolve().parents[1]
            evidence_dir = (repo_root / evidence).resolve()
            assert evidence_dir.is_relative_to(repo_root), "evidence directory must stay inside repository"
            stage_rows = db.all(
                "SELECT id,workspace_id,stage,status,started_at,finished_at,exit_code,tested_commit,stdout_tail,stderr_tail "
                "FROM test_runs WHERE workspace_type='agent' AND workspace_id IN (?,?) ORDER BY id",
                (workspace_id, backend_workspace_id),
            )
            stage_ledger = []
            for row in stage_rows:
                item = dict(row)
                for key in ("stdout_tail", "stderr_tail"):
                    if item[key]:
                        item[key] = item[key].replace("/work", "<container-project>").replace("/tmp/pytest-of-root", "<pytest-tmp>")
                stage_ledger.append(item)
            _preserve_evidence(evidence_dir, root, reference, captures, {
                "positiveHead": positive_head,
                "tamperCurrentHead": tamper_current_head,
                "negativeHead": negative_head,
                "dimensionMismatchHead": dimension_head,
                "missingReferenceHead": missing_head,
                "unavailableServerHead": unavailable_head,
                "backendHead": unavailable_head,
                "exitZeroWithoutEvidenceHead": no_evidence_head,
                "testStage": "PASS / exit 0 (real subprocess)",
                "positiveVisualStage": "PASS / exit 0 (real pinned UIFix CLI subprocess)",
                "negativeVisualStage": "FAIL / exit 1 (real color deviation)",
                "dimensionVisualStage": "FAIL / exit 2 (reference dimensions rejected)",
                "missingReference": f"TestRunner contract rejected before dispatch: {missing_reference_error}",
                "unavailableServer": "FAIL / exit 2 (real CLI SERVER_NOT_READY)",
                "backendOptOut": "PASS / only test stage dispatched",
                "exitZeroWithoutEvidence": "FAIL / exit 0 without valid run-scoped evidence",
                "tamperedEvidence": "FAIL after a current-head visual PASS was confirmed; not masked by stale SHA",
                "staleEvidence": "NOT_RUN after source HEAD changed",
                "captureRunIds": {
                    "positive": visual_row["id"],
                    "negativeColor": negative_visual_row["id"],
                    "tamperCurrentPositive": current_visual_row["id"],
                },
                "routeByCase": {
                    "positive": f"{initial_base_url}/same",
                    "negativeColor": f"{initial_base_url}/wrong",
                    "tamperCurrentPositive": f"{fixture.base_url}/same",
                },
                "projectFlowSourceCommit": os.environ.get("PF03E2_SOURCE_COMMIT", "UNSPECIFIED"),
                "projectFlowSourceTree": os.environ.get("PF03E2_SOURCE_TREE", "UNSPECIFIED"),
                "publishedPrHead": os.environ.get("PF03E2_PR_HEAD", "UNSPECIFIED"),
                "taskDecisionResults": decisions,
                "stageRuns": stage_ledger,
                "externalRequests": [],
                "httpMethods": sorted(set(method for method, _path in all_methods)),
            })
    finally:
        fixture.close()
