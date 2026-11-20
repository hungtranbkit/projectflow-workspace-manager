"""Ed25519 QA signatures: only the public verification key is needed on ProjectFlow."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app.services.quality_attestation_pilot import (
    payload_from_machine_report, sign_ed25519_receipt,
    verified_ed25519_receipt, quality_merge_blocker,
)

SHA = "a" * 40
DIGEST = "b" * 64
NOW = datetime(2026, 11, 21, 1, 0, tzinfo=timezone.utc)
TASK_ID = 17
REPO_ID = 22
NAME = "projectflow-workspace-manager"
TEST_FILE = "tests/test_harness_monitor_report.py"


def material():
    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def proof():
    report = {
        "status": "PASS", "test_exit_code": 0, "scope_dirty": False,
        "lint_violations": [], "git_head": SHA,
        "test_command": ["python", "-m", "pytest", "-q", TEST_FILE],
        "code_sha256": {"scripts/harness_monitor_report.py": DIGEST},
        "test_sha256": {TEST_FILE: DIGEST},
        "policy_sha256": DIGEST,
    }
    return payload_from_machine_report(report, task_id=TASK_ID,
                                       repository_id=REPO_ID,
                                       repository_name=NAME, now=NOW)


def verify(receipt, public_key, **changes):
    args = {"task_id": TASK_ID, "repository_id": REPO_ID,
            "repository_name": NAME, "expected_sha": SHA, "now": NOW}
    args.update(changes)
    return verified_ed25519_receipt(receipt, public_key, **args)


def test_signed_ed25519_proof_validates_exact_sha():
    private, public = material()
    signed = sign_ed25519_receipt(proof(), private)
    assert signed["signature_algorithm"] == "Ed25519"
    assert verify(signed, public) is None
    assert verify(signed, public, expected_sha="c"*40) == "QUALITY_ATTESTATION_STALE"
    assert verify(signed, public, now=NOW + timedelta(hours=25)) == "QUALITY_ATTESTATION_STALE"


def test_tamper_foreign_key_and_old_hmac_are_rejected():
    private, public = material()
    _, other_public = material()
    signed = sign_ed25519_receipt(proof(), private)
    signed["payload"]["status"] = "FAIL"
    assert verify(signed, public) == "QUALITY_ATTESTATION_INVALID"
    fresh = sign_ed25519_receipt(proof(), private)
    assert verify(fresh, other_public) == "QUALITY_ATTESTATION_INVALID"
    assert verify({"payload": proof(), "hmac_sha256": "a"*64}, public) == "QUALITY_ATTESTATION_INVALID"


def test_public_key_only_merge_gate(tmp_path):
    private, public = material()
    receipt = sign_ed25519_receipt(proof(), private)
    pubfile = tmp_path / "verifier-public.pem"
    pubfile.write_bytes(public)
    receipt_file = tmp_path / f"task-{TASK_ID}-repo-{REPO_ID}.json"
    receipt_file.write_text(json.dumps(receipt), encoding="utf8")
    env = {
        "PROJECTFLOW_QUALITY_PILOT_TASK_IDS": str(TASK_ID),
        "PROJECTFLOW_QUALITY_PILOT_REPO": NAME,
        "PROJECTFLOW_QUALITY_ATTESTATION_DIR": str(tmp_path),
        "PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE": str(pubfile),
    }
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) is None
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, "d"*40,
                                 environ=env, now=NOW) == "QUALITY_ATTESTATION_STALE"
    assert quality_merge_blocker(18, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) is None


def test_no_downgrade_on_missing_public_key(tmp_path):
    private, public = material()
    receipt = sign_ed25519_receipt(proof(), private)
    (tmp_path / f"task-{TASK_ID}-repo-{REPO_ID}.json").write_text(json.dumps(receipt))
    pubfile = tmp_path / "nonexistent-public.pem"
    env = {
        "PROJECTFLOW_QUALITY_PILOT_TASK_IDS": str(TASK_ID),
        "PROJECTFLOW_QUALITY_PILOT_REPO": NAME,
        "PROJECTFLOW_QUALITY_ATTESTATION_DIR": str(tmp_path),
        "PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE": str(pubfile),
    }
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) == "QUALITY_ATTESTATION_CONFIG"
    env["PROJECTFLOW_QUALITY_ATTESTATION_KEY"] = "legacy-hmac-key-"*4
    assert quality_merge_blocker(TASK_ID, REPO_ID, NAME, SHA,
                                 environ=env, now=NOW) == "QUALITY_ATTESTATION_CONFIG"


def test_rejects_non_ed25519_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    with pytest.raises(ValueError, match="Ed25519"):
        sign_ed25519_receipt(proof(), pem)
