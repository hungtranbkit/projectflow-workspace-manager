"""Isolated QA runs cannot inherit the signer identity or mount secrets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.agent_quality_isolated_runner import make_isolated_test_runner
from app.services.quality_attestation_pilot import (
    quality_merge_blocker, sign_ed25519_receipt,
)
from tests.test_quality_ed25519_pilot import material, proof, NOW, SHA, NAME, TASK_ID, REPO_ID


def _git(root, *argv):
    return subprocess.run(["git", *argv], cwd=root, check=True,
                          capture_output=True, text=True)


def test_image_reference_must_be_immutable():
    for bad in ("python:3.12-slim", "qa:latest", "", "sha256:zzz",
                "sha256:" + "a" * 63):
        with pytest.raises(ValueError, match="immutable"):
            make_isolated_test_runner(bad)
    assert callable(make_isolated_test_runner("sha256:" + "a" * 64))


def test_container_argv_snapshot_contains_no_git_or_signing_material(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n")
    (repo / "secret-untracked.txt").write_text("agent credential should never mount")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "qa@example.invalid")
    _git(repo, "config", "user.name", "QA")
    _git(repo, "add", "tests/test_ok.py")
    _git(repo, "commit", "-m", "demo")
    import scripts.agent_quality_isolated_runner as module
    orig_run = module.subprocess.run
    seen = []

    def fake_run(argv, *args, **kwargs):
        if argv[:2] == ["docker", "run"]:
            seen.append((argv, kwargs))
            assert "--network=none" in argv
            assert "--read-only" in argv
            assert "--cap-drop=ALL" in argv
            assert "--security-opt=no-new-privileges" in argv
            assert "--pull=never" in argv
            assert "--user=65534:65534" in argv
            # Importing ProjectFlow's app package creates a SQLite DB
            # during pytest collection. Keep it on the bounded tmpfs,
            # never in read-only /work/data.
            assert "WORKSPACE_MANAGER_ROOT=/tmp" in argv
            assert "WORKSPACE_MANAGER_DB=/tmp/qg-state/workspace-manager.db" in argv
            assert "WORKSPACE_MANAGER_STATE_DIR=/tmp/qg-state/runtime" in argv
            assert "WORKSPACE_MANAGER_WORKTREE_ROOT=/tmp/qg-worktrees" in argv
            assert "-p" in argv and "no:cacheprovider" in argv
            assert "--mount" in argv
            mount = argv[argv.index("--mount") + 1]
            source = Path(mount.split("source=", 1)[1].split(",", 1)[0])
            assert (source / "tests/test_ok.py").is_file()
            assert not (source / ".git").exists()
            assert not (source / "secret-untracked.txt").exists()
            assert "PROJECTFLOW_QUALITY_ATTESTATION_KEY" not in kwargs["env"]
            assert "PROJECTFLOW_QUALITY_ATTESTATION_PRIVATE_KEY_FILE" not in kwargs["env"]
            assert argv[-1] == "tests/test_ok.py"
            kwargs["stdout"].write(b"1 passed\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return orig_run(argv, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    process = make_isolated_test_runner("sha256:" + "a" * 64)(
        ["python", "-m", "pytest", "-q", "tests/test_ok.py"], repo,
        {"PROJECTFLOW_QUALITY_ATTESTATION_KEY": "not-for-docker"})
    assert process.returncode == 0
    assert "1 passed" in process.stdout
    assert len(seen) == 1


def test_signed_isolated_attestation_required_without_hmac_fallback(tmp_path):
    private, public = material()
    pubfile = tmp_path / "verifier-public.pem"
    pubfile.write_bytes(public)
    receipt_path = tmp_path / f"task-{TASK_ID}-repo-{REPO_ID}.json"
    env = {
        "PROJECTFLOW_QUALITY_PILOT_TASK_IDS": str(TASK_ID),
        "PROJECTFLOW_QUALITY_PILOT_REPO": NAME,
        "PROJECTFLOW_QUALITY_ATTESTATION_DIR": str(tmp_path),
        "PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE": str(pubfile),
        "PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA": "1",
        "PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID": "sha256:" + "c" * 64,
    }
    host_only = sign_ed25519_receipt(proof(), private)
    receipt_path.write_text(json.dumps(host_only))
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) == "QUALITY_ATTESTATION_INVALID"
    payload = proof()
    payload["qa_isolation"] = "DOCKER_READ_ONLY_NO_NETWORK"
    payload["qa_image_id"] = "sha256:" + "c" * 64
    receipt_path.write_text(json.dumps(sign_ed25519_receipt(payload, private)))
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) is None
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, "d" * 40,
                                 environ=env, now=NOW) == "QUALITY_ATTESTATION_STALE"


def test_strict_verifier_does_not_trust_unsigned_or_unapproved_image(tmp_path):
    """An image SHA merely asserted by the signer is not the verifier's pin."""
    private, public = material()
    pubfile = tmp_path / "verifier-public.pem"
    pubfile.write_bytes(public)
    env = {
        "PROJECTFLOW_QUALITY_PILOT_TASK_IDS": str(TASK_ID),
        "PROJECTFLOW_QUALITY_PILOT_REPO": NAME,
        "PROJECTFLOW_QUALITY_ATTESTATION_DIR": str(tmp_path),
        "PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE": str(pubfile),
        "PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA": "1",
    }
    receipt_path = tmp_path / f"task-{TASK_ID}-repo-{REPO_ID}.json"
    payload = proof()
    payload["qa_isolation"] = "DOCKER_READ_ONLY_NO_NETWORK"
    payload["qa_image_id"] = "sha256:" + "a" * 64
    receipt_path.write_text(json.dumps(sign_ed25519_receipt(payload, private)))

    # Strict verification fails closed without an independently configured
    # image pin, even for a signature that is otherwise valid.
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) == "QUALITY_ATTESTATION_CONFIG"
    env["PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID"] = "python:latest"
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) == "QUALITY_ATTESTATION_CONFIG"
    env["PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID"] = "sha256:" + "b" * 64
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) == "QUALITY_ATTESTATION_STALE"
    env["PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID"] = payload["qa_image_id"]
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) is None


def test_strict_isolation_never_falls_back_to_legacy_hmac(tmp_path):
    from app.services.quality_attestation_pilot import sign_receipt
    payload = proof()
    payload["qa_isolation"] = "DOCKER_READ_ONLY_NO_NETWORK"
    payload["qa_image_id"] = "sha256:" + "a" * 64
    key = "unit-test-only-hmac-secret-that-is-at-least-32-characters"
    receipt_path = tmp_path / f"task-{TASK_ID}-repo-{REPO_ID}.json"
    receipt_path.write_text(json.dumps(sign_receipt(payload, key)))
    env = {
        "PROJECTFLOW_QUALITY_PILOT_TASK_IDS": str(TASK_ID),
        "PROJECTFLOW_QUALITY_PILOT_REPO": NAME,
        "PROJECTFLOW_QUALITY_ATTESTATION_DIR": str(tmp_path),
        "PROJECTFLOW_QUALITY_ATTESTATION_KEY": key,
        "PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA": "1",
        "PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID": payload["qa_image_id"],
    }
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) == "QUALITY_ATTESTATION_CONFIG"
