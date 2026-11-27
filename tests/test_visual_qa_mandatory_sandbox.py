"""Real AUTH_MODE=required visual QA through TestRunner -> Docker sandbox.

The fixture starts its HTTP server *inside the same network-none container*
as UIFix. No TestRunner, SandboxRuntimeService, Docker, browser, or CLI result
is mocked. Runtime observers below delegate to the existing implementation.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
import shlex

import pytest
import yaml

from app.config import load_settings
from app.main import create_app
from app.services.project_contract import ContractError
from app.services.visual_qa_evidence import ensure_private_directory
from tests.test_visual_qa_runner_subprocess import _add_task_and_workspace, _commit, _run_stages


UIFIX_SHA = "97c4b9d83684c7d607cb8c4b146e3f8aa5356e53"
PLAYWRIGHT_IMAGE = (
    "mcr.microsoft.com/playwright:v1.60.0-noble@"
    "sha256:83192064c7510f7ee73dd63dc5f22a5e01a92c81a2e6a9c715d9e3fe55471fd9"
)
PORT = 43871


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker is required for AUTH_MODE=required integration")


def _fixture_server_source() -> str:
    return '''from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys

port = int(sys.argv[1])
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        color = "#18395a" if self.path in ("/same", "/health") else "#a53179"
        page = ("<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<style>*{{box-sizing:border-box}}html,body{{margin:0;width:100%;height:100%;background:{color}}}</style>"
                "</head><body></body></html>").encode()
        self.send_response(200); self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(page))); self.end_headers(); self.wfile.write(page)
    def log_message(self, *_args):
        pass

ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def _server_ready_command() -> str:
    code = f'''import time,urllib.request
for attempt in range(40):
    try:
        urllib.request.urlopen("http://127.0.0.1:{PORT}/health", timeout=0.2).read()
        break
    except Exception:
        if attempt == 39: raise
        time.sleep(0.1)
'''
    return shlex.join(["python3", "-c", code])


def _visual_command(route: str) -> str:
    server = shlex.join(["python3", "tools/visual_qa/sandbox-fixture-server.py", str(PORT)])
    cli = shlex.join([
        "node", "tools/visual_qa/visual-qa.mjs", "--reference", "docs/reference.png",
        "--url", f"http://127.0.0.1:{PORT}{route}", "--viewport", "320x240",
        "--out", ".runtime/standalone-output-is-overridden-by-TestRunner", "--threshold", "0.08",
    ])
    trap = 'trap \'kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true\' EXIT'
    return (
        "set -eu; "
        + server + " > .runtime/sandbox-fixture-server.log 2>&1 & server_pid=$!; "
        + trap + "; " + _server_ready_command() + "; " + cli
    )


def _write_contract(root: Path, route: str, command: str, *, reference: str = "docs/reference.png",
                    timeout: int = 40, memory: str = "3g") -> None:
    contract = {
        "schema_version": 1,
        "project": {"code": "pf03e2-mandatory-fixture"},
        "source": {"root": "."},
        "commands": {
            "test": {"command": "python3 -c 'print(\"mandatory-fixture-test-ran\")'", "timeout_seconds": 30},
            "visual_qa": {"command": command, "timeout_seconds": timeout},
        },
        "ci": {"required": ["test"]},
        "visual_qa": {
            "enabled": True,
            "reference": reference,
            "route": f"http://127.0.0.1:{PORT}{route}",
            "viewports": [{"width": 320, "height": 240}],
            "threshold": 0.08,
        },
        "exec_sandbox": {
            "image": PLAYWRIGHT_IMAGE,
            "network": "none",
            "memory": memory,
            "cpus": "2.0",
            "pids_limit": 256,
        },
    }
    (root / "PROJECT.yaml").write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")


def _run_decision(app, workspace_id: int, task_id: int, root: Path, head: str) -> str:
    return app.state.decision.builder_tests_status({
        "id": workspace_id, "task_id": task_id, "head": head, "worktree_path": str(root),
    })


def _runtime_calls(runtime) -> list[list[str]]:
    return [row for row in runtime.recorded_argv if len(row) > 1 and row[1] == "run"]


def _assert_and_capture_sandbox_argv(calls: list[list[str]], root: Path, *, secret_values: tuple[str, ...]) -> list[str]:
    names: list[str] = []
    for argv in calls:
        assert "--rm" in argv
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--memory") + 1] in {"3g", "not-a-size"}
        assert argv[argv.index("--cpus") + 1] == "2.0"
        assert argv[argv.index("--pids-limit") + 1] == "256"
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
        owner = root.stat()
        assert argv[argv.index("--user") + 1] == f"{owner.st_uid}:{owner.st_gid}"
        assert argv[argv.index("-v") + 1] == f"{root}:/workspace:rw"
        assert argv[argv.index("-w") + 1] == "/workspace"
        assert argv.count("-v") == 1
        assert argv.count("-e") in (0, 3)
        if "-e" in argv:
            injected = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-e"]
            assert {value.split("=", 1)[0] for value in injected} == {
                "VISUAL_QA_RUN_ID", "VISUAL_QA_TESTED_COMMIT", "VISUAL_QA_EVIDENCE_OUTPUT",
            }
            assert any(value.startswith("VISUAL_QA_EVIDENCE_OUTPUT=/workspace/.runtime/projectflow-visual-qa/") for value in injected)
        assert argv[argv.index("--name") + 1].startswith("wm-exec-")
        names.append(argv[argv.index("--name") + 1])
        assert PLAYWRIGHT_IMAGE in argv
        assert not any(secret and secret in value for secret in secret_values for value in argv)
    return names


def _save_mandatory_evidence(output: Path, root: Path, captures: dict[str, Path], rows: list[dict],
                             runtime, details: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    copied: dict[str, dict] = {}
    for label, directory in captures.items():
        summary = {}
        for name in ("actual.png", "diff.png", "overlay.png", "metrics.json", "manifest.json"):
            file = directory / name
            if file.is_file():
                dest = output / f"{label}-{name}"
                shutil.copyfile(file, dest)
                summary[dest.name] = hashlib.sha256(dest.read_bytes()).hexdigest()
        copied[label] = summary
    if (root / "docs/reference.png").is_file():
        shutil.copyfile(root / "docs/reference.png", output / "reference.png")
    safe_calls = []
    for argv in _runtime_calls(runtime):
        safe_calls.append([arg.replace(str(root), "<project-root>") for arg in argv])
    safe_control_calls = []
    for argv in runtime.recorded_argv:
        if len(argv) > 1 and argv[1] != "run":
            safe_control_calls.append([arg.replace(str(root), "<project-root>") for arg in argv])
    (output / "mandatory-results.json").write_text(json.dumps({
        **details,
        "fixtureCommit": runtime.source_commit,
        "fixtureTree": runtime.source_tree,
        "image": PLAYWRIGHT_IMAGE,
        "containerCommands": safe_calls,
        "containerControlCommands": safe_control_calls,
        "captureHashes": copied,
        "stageRows": rows,
    }, indent=2) + "\n", encoding="utf-8")
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(output.iterdir()) if p.is_file() and p.name != "SHA256SUMS.json"}
    (output / "SHA256SUMS.json").write_text(json.dumps(files, indent=2) + "\n", encoding="utf-8")


def test_required_auth_visual_gate_runs_inside_real_sandbox(tmp_path, monkeypatch):
    """AUTH_MODE=required -> app wiring -> mandatory runner -> real Docker run.

    Runs a same-container fixture server because the real sandbox network is
    intentionally none. Artifact output uses TestRunner's private run path.
    """
    project_root = tmp_path / "mandatory-project"
    project_root.mkdir(mode=0o755)
    project_root.chmod(0o755)

    # Test-only auth values are app construction inputs and are never passed to
    # a sandbox command. They are fixed synthetic values, not credentials.
    monkeypatch.setenv("WORKSPACE_MANAGER_AUTH_MODE", "required")
    monkeypatch.setenv("WORKSPACE_MANAGER_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKSPACE_MANAGER_WORKTREE_ROOT", str(tmp_path / ".worktrees"))
    monkeypatch.setenv("WORKSPACE_MANAGER_DB", str(tmp_path / "coordinator.sqlite"))
    monkeypatch.setenv("WORKSPACE_MANAGER_STATE_DIR", str(tmp_path / "coordinator-state"))
    session_secret = "pf03e2-test-only-session-secret-no-real-credential"
    encryption_key = "M2RXNV3dhIR-lc1WoE8DGxt-kowfK-34xGTIcF1t8m4="
    monkeypatch.setenv("WORKSPACE_MANAGER_SESSION_SECRET", session_secret)
    monkeypatch.setenv("WORKSPACE_MANAGER_SECRET_ENCRYPTION_KEYS", encryption_key)
    settings = load_settings()
    assert settings.auth_mode == "required"
    app = create_app(settings)
    assert app.state.settings.auth_mode == "required"
    assert app.state.sandboxed_exec.mandatory is True
    assert app.state.runner.runner is app.state.sandboxed_exec

    runtime = app.state.sandboxed_exec.sandbox_runtime
    real_docker_run = runtime._run
    runtime.recorded_argv = []

    def record_actual_call(argv, cwd, timeout=None):
        runtime.recorded_argv.append(list(argv))
        return real_docker_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(runtime, "_run", record_actual_call)
    runtime.source_commit = "UNSET"
    runtime.source_tree = "UNSET"
    actual_runner = app.state.sandboxed_exec
    original_run = actual_runner.run
    capture_root = tmp_path / "mandatory-captured"
    captures: dict[str, Path] = {}
    decisions: dict[str, str] = {}

    def observe_real_execution(command, worktree_root, working_dir, timeout, env=None):
        result = original_run(command, worktree_root, working_dir, timeout, env=env)
        run_id = (env or {}).get("VISUAL_QA_RUN_ID")
        if run_id:
            source = Path(worktree_root).absolute() / ".runtime" / "projectflow-visual-qa" / str(run_id)
            if source.is_dir() and any(source.iterdir()):
                target = capture_root / str(run_id)
                shutil.copytree(source, target)
                captures[str(run_id)] = target / "320x240"
        return result

    monkeypatch.setattr(actual_runner, "run", observe_real_execution)

    (project_root / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    (project_root / "README.md").write_text("disposable mandatory visual fixture\n", encoding="utf-8")
    cli = Path(__file__).resolve().parents[1] / "tools" / "visual_qa" / "visual-qa.mjs"
    (project_root / "tools/visual_qa").mkdir(parents=True)
    shutil.copyfile(cli, project_root / "tools/visual_qa/visual-qa.mjs")
    launch_policy = Path(__file__).resolve().parents[1] / "tools" / "visual_qa" / "browser-launch.mjs"
    shutil.copyfile(launch_policy, project_root / "tools/visual_qa/browser-launch.mjs")
    server_script = project_root / "tools/visual_qa/sandbox-fixture-server.py"
    server_script.write_text(_fixture_server_source(), encoding="utf-8")
    (project_root / ".runtime/uifix-v1.2.6").parent.mkdir(parents=True)
    shutil.copytree(Path(__file__).resolve().parents[1] / ".runtime/uifix-v1.2.6",
                    project_root / ".runtime/uifix-v1.2.6", symlinks=True)
    reference = project_root / "docs/reference.png"
    reference.parent.mkdir(parents=True)
    sharp_package = project_root / ".runtime/uifix-v1.2.6/package.json"
    create_reference = (
        "const {createRequire}=require('node:module');"
        f"const sharp=createRequire({json.dumps(str(sharp_package))})('sharp');"
        f"sharp({{create:{{width:320,height:240,channels:3,background:{{r:24,g:57,b:90}}}}}})"
        f".png().toFile({json.dumps(str(reference))}).catch(e=>{{console.error(e);process.exitCode=1}});"
    )
    subprocess.run(["node", "-e", create_reference], check=True, text=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "qa@example.invalid"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "PF-03e2 mandatory fixture"], cwd=project_root, check=True, capture_output=True)
    _write_contract(project_root, "/same", _visual_command("/same"))
    subprocess.run(["git", "add", ".gitignore", "README.md", "tools/visual_qa", "docs/reference.png", "PROJECT.yaml"],
                   cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "mandatory visual fixture base"], cwd=project_root,
                   check=True, capture_output=True)

    app_git = app.state.git
    head = app_git.head(project_root)
    task_id, workspace_id = _add_task_and_workspace(
        app.state.db, app_git, project_root, "Synthetic UI task\nVISUAL_QA_REQUIRED: true", "mandatory-ui",
    )
    runtime.source_commit = head
    runtime.source_tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=project_root,
                                         check=True, capture_output=True, text=True).stdout.strip()

    artifact_dir_env = os.environ.get("VISUAL_QA_MANDATORY_EVIDENCE_DIR")
    evidence_output = Path(artifact_dir_env).resolve() if artifact_dir_env else tmp_path / "mandatory-evidence"
    evidence_output.mkdir(parents=True, exist_ok=True)
    details: dict = {
        "projectFlowSourceCommit": os.environ.get("PF03E2_PROJECTFLOW_SOURCE_SHA", "UNSPECIFIED"),
        "projectFlowSourceTree": os.environ.get("PF03E2_PROJECTFLOW_SOURCE_TREE", "UNSPECIFIED"),
        "authMode": settings.auth_mode,
        "mandatory": actual_runner.mandatory,
        "uifixSha": UIFIX_SHA,
        "referenceSha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
    }
    try:
        positive_rows = _run_stages(app.state.runner, app.state.db, workspace_id, project_root)
        assert [(row["stage"], row["status"], row["exit_code"]) for row in positive_rows] == [
            ("test", "PASS", 0), ("visual_qa", "PASS", 0),
        ], positive_rows
        positive_visual = next(row for row in positive_rows if row["stage"] == "visual_qa")
        decisions["positive"] = _run_decision(app, workspace_id, task_id, project_root, head)
        assert decisions["positive"] == "PASS"
        assert str(positive_visual["tested_commit"]) == head
        positive_artifacts = captures[str(positive_visual["id"])]
        positive_metrics = json.loads((positive_artifacts / "metrics.json").read_text())
        assert positive_metrics["toolCommit"] == UIFIX_SHA
        assert positive_metrics["passed"] is True and positive_metrics["percentDiff"] == 0
        assert positive_metrics["mismatchedPixels"] == 0
        assert positive_metrics["blockedRequests"] == [] and positive_metrics["pageErrors"] == []
        decisions["tamperBefore"] = decisions["positive"]
        coordinator_actual = (app.state.runner.visual_evidence_root / "agent" / str(workspace_id)
                              / str(positive_visual["id"]) / "320x240" / "actual.png")
        coordinator_actual.write_bytes(b"tampered current mandatory visual evidence")
        decisions["tamperAfter"] = _run_decision(app, workspace_id, task_id, project_root, head)
        assert decisions["tamperAfter"] == "FAIL"

        (project_root / "revision.txt").write_text("advance source to stale the original PASS\n", encoding="utf-8")
        stale_head = _commit(project_root, app_git, "revision.txt", message="advance mandatory fixture source")
        decisions["stale"] = _run_decision(app, workspace_id, task_id, project_root, stale_head)
        assert decisions["stale"] == "NOT_RUN"

        _write_contract(project_root, "/wrong", _visual_command("/wrong"))
        negative_head = _commit(project_root, app_git, "PROJECT.yaml", message="intentional mandatory color deviation")
        negative_rows = _run_stages(app.state.runner, app.state.db, workspace_id, project_root)
        assert [(row["stage"], row["status"], row["exit_code"]) for row in negative_rows] == [
            ("test", "PASS", 0), ("visual_qa", "FAIL", 1),
        ], negative_rows
        negative_visual = next(row for row in negative_rows if row["stage"] == "visual_qa")
        decisions["negative"] = _run_decision(app, workspace_id, task_id, project_root, negative_head)
        assert decisions["negative"] == "FAIL"
        negative_artifacts = captures[str(negative_visual["id"])]
        negative_metrics = json.loads((negative_artifacts / "metrics.json").read_text())
        assert negative_metrics["passed"] is False and negative_metrics["percentDiff"] > 0.08

        run_count_before_missing = len(app.state.db.all(
            "SELECT id FROM test_runs WHERE workspace_type='agent' AND workspace_id=?", (workspace_id,),
        ))
        _write_contract(project_root, "/same", _visual_command("/same"), reference="docs/missing.png")
        missing_head = _commit(project_root, app_git, "PROJECT.yaml", message="mandatory missing reference")
        calls_before_missing = len(runtime.recorded_argv)
        with pytest.raises(ContractError, match="does not exist"):
            app.state.runner.start("agent", workspace_id, project_root)
        assert len(runtime.recorded_argv) == calls_before_missing
        assert len(app.state.db.all(
            "SELECT id FROM test_runs WHERE workspace_type='agent' AND workspace_id=?", (workspace_id,),
        )) == run_count_before_missing
        decisions["missingReference"] = _run_decision(app, workspace_id, task_id, project_root, missing_head)
        assert decisions["missingReference"] == "NOT_RUN"

        _write_contract(project_root, "/same", "true")
        no_artifact_head = _commit(project_root, app_git, "PROJECT.yaml", message="zero exit without visual evidence")
        no_artifact_rows = _run_stages(app.state.runner, app.state.db, workspace_id, project_root)
        assert [(row["stage"], row["status"], row["exit_code"]) for row in no_artifact_rows] == [
            ("test", "PASS", 0), ("visual_qa", "FAIL", 0),
        ], no_artifact_rows
        decisions["zeroExitNoEvidence"] = _run_decision(app, workspace_id, task_id, project_root, no_artifact_head)
        assert decisions["zeroExitNoEvidence"] == "FAIL"

        _write_contract(project_root, "/same", _visual_command("/same"), memory="not-a-size")
        startup_head = _commit(project_root, app_git, "PROJECT.yaml", message="sandbox startup failure fixture")
        startup_rows = _run_stages(app.state.runner, app.state.db, workspace_id, project_root)
        assert startup_rows[0]["status"] == "FAIL" and "invalid size" in (startup_rows[0]["stderr_tail"] or "").lower()
        assert startup_rows[1]["status"] == "SKIPPED"
        decisions["startupFailure"] = _run_decision(app, workspace_id, task_id, project_root, startup_head)
        assert decisions["startupFailure"] == "NOT_RUN"

        _write_contract(project_root, "/same", "sleep 30", timeout=3)
        timeout_head = _commit(project_root, app_git, "PROJECT.yaml", message="bounded sandbox timeout fixture")
        timeout_rows = _run_stages(app.state.runner, app.state.db, workspace_id, project_root)
        assert timeout_rows[0]["status"] == "PASS"
        assert timeout_rows[1]["status"] == "TIMEOUT" and timeout_rows[1]["exit_code"] == 124
        decisions["timeout"] = _run_decision(app, workspace_id, task_id, project_root, timeout_head)
        assert decisions["timeout"] == "FAIL"

        timeout_argv = next(argv for argv in _runtime_calls(runtime) if argv[-1] == "sleep 30")
        timeout_name = timeout_argv[timeout_argv.index("--name") + 1]
        assert any(len(argv) > 2 and argv[1:3] == ["kill", timeout_name] for argv in runtime.recorded_argv)
        assert any(len(argv) > 2 and argv[1:3] == ["rm", "-f"] and argv[-1] == timeout_name
                   for argv in runtime.recorded_argv)

        actual_calls = _runtime_calls(runtime)
        names = _assert_and_capture_sandbox_argv(
            actual_calls, project_root, secret_values=(session_secret, encryption_key),
        )
        details.update({
            "fixtureSourceCommit": runtime.source_commit,
            "fixtureSourceTree": runtime.source_tree,
            "testedFixtureHeads": {"positive": head, "negative": negative_head,
                                   "missingReference": missing_head, "exitZeroNoEvidence": no_artifact_head,
                                   "startupFailure": startup_head, "timeout": timeout_head},
            "taskDecisionResults": decisions,
            "containerNames": names,
            "containerCount": len(actual_calls),
            "loopbackOnly": True,
            "productionOrProviderCalls": False,
        })
        assert actual_calls and all(not subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip() for name in names)
    finally:
        rows = app.state.db.all(
            "SELECT id,stage,status,exit_code,tested_commit,stdout_tail,stderr_tail FROM test_runs "
            "WHERE workspace_type='agent' AND workspace_id=? ORDER BY id", (workspace_id,),
        )
        clean_rows = []
        for row in rows:
            item = dict(row)
            for key in ("stdout_tail", "stderr_tail"):
                if item[key]:
                    item[key] = item[key].replace(str(project_root), "<project-root>")
            clean_rows.append(item)
        _save_mandatory_evidence(evidence_output, project_root, captures, clean_rows, runtime, {
            **details,
            "taskDecisionResults": decisions,
            "fixtureSourceCommit": runtime.source_commit,
            "fixtureSourceTree": runtime.source_tree,
        })
