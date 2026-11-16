"""No-network fixture tests for pinned Zalo Hub Node/JDK21 Docker QA."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from scripts import harness_zgh_node_java_qa as qa
from app.services.agent_harness_mvp import HarnessError
from scripts import harness_monitor_bridge as bridge


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/v2-source-integration-tests.sh").write_text(
        "#!/usr/bin/env bash\nset -e\necho PASS\n")
    (root / "source.cjs").write_text("console.log('safe')\n")
    subprocess.run(["git", "-C", str(root), "init", "-b", "harness/v2-qa"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=qa@localhost",
                    "-c", "user.name=QA", "commit", "-m", "baseline"],
                   check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return root, head


def _spec(root, head, run_dir):
    script = root / qa.RUNNER
    return dict(repo=str(root), base_sha=head, executor="zgh-node-java-qa",
                expected=qa.RUNNER,
                test_sha=hashlib.sha256(script.read_bytes()).hexdigest(),
                run_dir=str(run_dir),
                created_at=datetime.now(timezone.utc).isoformat(),
                ttl_seconds=300)


def test_source_snapshot_uses_committed_blob_and_rejects_symlink(repo, tmp_path):
    root, head = repo
    (root / "source.cjs").write_text("uncommitted change\n")
    manifest, count = qa.pinned_snapshot(root, head, tmp_path / "safe")
    assert count == 2
    assert len(manifest) == 64
    assert (tmp_path / "safe/source.cjs").read_text() == "console.log('safe')\n"
    # A tracked symlink is never accepted into a sandbox bind mount.
    subprocess.run(["git", "-C", str(root), "checkout", "--", "source.cjs"],
                   check=True, capture_output=True)
    (root / "unsafe").symlink_to("/etc/passwd")
    subprocess.run(["git", "-C", str(root), "add", "unsafe"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=qa@localhost",
                    "-c", "user.name=QA", "commit", "-m", "tracked symlink"],
                   check=True, capture_output=True)
    new = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    with pytest.raises(RuntimeError, match="unsafe tracked entry"):
        qa.pinned_snapshot(root, new, tmp_path / "refused")


def test_offline_docker_command_is_privilege_restricted(repo, tmp_path, monkeypatch):
    root, head = repo
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snap = run_dir / "snap"
    qa.pinned_snapshot(root, head, snap)
    fake_jdk = tmp_path / "jdk"
    (fake_jdk / "bin").mkdir(parents=True)
    (fake_jdk / "bin/java").write_text("java")
    jar = tmp_path / "orgjson"
    jar.mkdir()
    monkeypatch.setattr(qa, "JDK21", fake_jdk)
    monkeypatch.setattr(qa, "ORG_JSON", jar)
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=0,
                 stdout=json.dumps([{"Id": qa.IMAGE_ID}]))
        return SimpleNamespace(returncode=0, stdout="java and node pass", stderr="")
    result = qa.sandbox_qa(_spec(root, head, run_dir), snap, run_dir, runner=runner)
    assert result.returncode == 0
    docker = calls[-1]
    assert "--network=none" in docker
    assert "--read-only" in docker
    assert "--cap-drop=ALL" in docker
    assert "--security-opt=no-new-privileges" in docker
    assert "--pull=never" in docker
    assert "src=" + str(snap.resolve()) + ",dst=/repo,readonly" in docker
    assert qa.IMAGE_ID in docker


def test_independent_qa_proof_rejects_worktree_drift(repo, tmp_path, monkeypatch):
    root, head = repo
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spec = _spec(root, head, run_dir)
    (run_dir / "job.json").write_text(json.dumps(spec))
    def ok(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="java pass\nnode pass\n", stderr="")
    monkeypatch.setattr(qa, "sandbox_qa", ok)
    report = qa.run_verified_qa(spec, run_dir)
    assert report["qa_pass"] is True
    assert report["test_exit"] == 0
    row = dict(repo=str(root), base_sha=head, test_sha=spec["test_sha"],
               expected_sha=hashlib.sha256(qa.RUNNER.encode()).hexdigest())
    assert qa.independently_verify(row, report, run_dir) is True
    (root / "source.cjs").write_text("a new unreviewed delta")
    assert qa.independently_verify(row, report, run_dir) is False


def test_readonly_qa_worker_does_not_claim_business_e2e():
    assert qa.PROOF.startswith("HARNESS_ZGH_NODE_JAVA_QA")
    assert "APK" not in qa.PROOF
    assert "S7" not in qa.PROOF


def test_bridge_qa_only_for_enabled_zalo_monitor(tmp_path):
    source = bridge.load_registry()
    active = bridge.ZGH_V2_AUTOMATION_ID
    assert source[active]["configured_enabled"]
    # Explicit fixed script is the only accepted TestDoD; no arbitrary shell.
    with pytest.raises(HarnessError, match="pinned integration script"):
        bridge.launch_managed_child(source, tmp_path / "not-needed.db", active,
            child_goal="Node QA", repo_dir=tmp_path, executor="zgh-node-java-qa",
            test_path="../evil.sh")
