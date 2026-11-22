"""Read-only Quality Gate preflight: no secrets, no writes, no false PASS."""
from __future__ import annotations

from datetime import timedelta
import json

from app.services.quality_attestation_pilot import sign_ed25519_receipt
from scripts.quality_gate_readiness import evaluate
from tests.test_quality_ed25519_pilot import (
    material, proof, NOW, SHA, NAME, TASK_ID, REPO_ID,
)

IMAGE = "sha256:" + "a" * 64


def configured(tmp_path):
    private, public = material()
    key_path = tmp_path / "public.pem"
    key_path.write_bytes(public)
    return private, {
        "PROJECTFLOW_QUALITY_PILOT_TASK_IDS": str(TASK_ID),
        "PROJECTFLOW_QUALITY_PILOT_REPO": NAME,
        "PROJECTFLOW_QUALITY_ATTESTATION_DIR": str(tmp_path),
        "PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE": str(key_path),
        "PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA": "1",
        "PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID": IMAGE,
    }


def signed(tmp_path, private, *, image=IMAGE):
    payload = proof()
    payload["qa_isolation"] = "DOCKER_READ_ONLY_NO_NETWORK"
    payload["qa_image_id"] = image
    (tmp_path / f"task-{TASK_ID}-repo-{REPO_ID}.json").write_text(
        json.dumps(sign_ed25519_receipt(payload, private))
    )


def probe(env, now=NOW, sha=SHA, task_id=TASK_ID):
    return evaluate(env, task_id=task_id, repository_id=REPO_ID,
                    repository_name=NAME, expected_sha=sha, now=now)


def test_disabled_is_never_a_protected_merge():
    result = probe({})
    assert result["status"] == "DISABLED"
    assert result["enabled"] is False
    assert result["production_end_to_end_accepted"] is False
    assert result["probe"]["status"] == "OUT_OF_SCOPE_NOT_PROTECTED"


def test_strict_configuration_and_signature_are_diagnostic_only(tmp_path):
    private, env = configured(tmp_path)
    signed(tmp_path, private)
    result = probe(env)
    assert result["status"] == "CONFIGURED_NEEDS_EXTERNAL_ACCEPTANCE"
    assert result["configuration_issues"] == []
    assert result["probe"]["status"] == "VERIFIER_ACCEPTS_GIVEN_INPUT"
    assert result["probe"]["blocker"] is None
    assert result["probe"]["receipt"]["present"] is True
    assert result["probe"]["receipt"]["fresh"] is True
    assert result["probe"]["receipt"]["image_claim_matches_pin"] is True
    assert result["probe"]["live_merge_authorized"] is False
    assert result["production_end_to_end_accepted"] is False
    assert result["independent_qa_principal_proven"] is False


def test_strict_missing_receipt_fail_closed(tmp_path):
    _, env = configured(tmp_path)
    result = probe(env)
    assert result["probe"]["blocker"] == "QUALITY_ATTESTATION_MISSING"
    assert result["probe"]["status"] == "VERIFIER_BLOCKS_GIVEN_INPUT"


def test_wrong_commit_and_expired_receipt_are_not_accepted(tmp_path):
    private, env = configured(tmp_path)
    signed(tmp_path, private)
    assert probe(env, sha="b"*40)["probe"]["blocker"] == "QUALITY_ATTESTATION_STALE"
    expired = probe(env, now=NOW + timedelta(hours=25))
    assert expired["probe"]["blocker"] == "QUALITY_ATTESTATION_STALE"
    assert expired["probe"]["receipt"]["fresh"] is False


def test_wrong_image_is_independently_detected(tmp_path):
    private, env = configured(tmp_path)
    signed(tmp_path, private, image="sha256:"+"b"*64)
    result = probe(env)
    assert result["probe"]["blocker"] == "QUALITY_ATTESTATION_STALE"
    assert result["probe"]["receipt"]["image_claim_matches_pin"] is False


def test_missing_image_and_signing_private_key_inside_verifier_are_reported(tmp_path):
    private, env = configured(tmp_path)
    signed(tmp_path, private)
    env.pop("PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID")
    env["PROJECTFLOW_QUALITY_ATTESTATION_PRIVATE_KEY_FILE"] = "/private/path-not-shown"
    report = probe(env)
    assert report["status"] == "CONFIGURATION_BLOCKED"
    assert "VERIFIER_APPROVED_IMAGE_NOT_PINNED" in report["configuration_issues"]
    assert "PRIVATE_SIGNING_KEY_IN_VERIFIER_ENVIRONMENT" in report["configuration_issues"]
    assert "/private/path-not-shown" not in json.dumps(report)


def test_non_strict_is_not_production_accepted(tmp_path):
    private, env = configured(tmp_path)
    signed(tmp_path, private)
    env["PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA"] = "0"
    report = probe(env)
    assert "ISOLATED_QA_NOT_REQUIRED" in report["configuration_issues"]
    assert report["production_end_to_end_accepted"] is False


def test_bad_selection_and_missing_scope_fail_closed(tmp_path):
    _, env = configured(tmp_path)
    env["PROJECTFLOW_QUALITY_PILOT_TASK_IDS"] = "17,17"
    env.pop("PROJECTFLOW_QUALITY_PILOT_REPO")
    report = probe(env)
    assert report["status"] == "CONFIGURATION_BLOCKED"
    assert "INVALID_TASK_SELECTION" in report["configuration_issues"]
    assert "MISSING_REPOSITORY_SCOPE" in report["configuration_issues"]


def test_unselected_task_not_confused_with_valid_proof(tmp_path):
    private, env = configured(tmp_path)
    signed(tmp_path, private)
    report = probe(env, task_id=TASK_ID + 1)
    assert report["probe"]["status"] == "OUT_OF_SCOPE_NOT_PROTECTED"
    assert report["production_end_to_end_accepted"] is False


def test_secret_never_appears_in_monitor_output(tmp_path):
    private, env = configured(tmp_path)
    signed(tmp_path, private)
    secret = "sample-sensitive-legacy-hmac-secret-" + "z"*32
    env["PROJECTFLOW_QUALITY_ATTESTATION_KEY"] = secret
    report = probe(env)
    assert secret not in json.dumps(report)
    assert env["PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE"] not in json.dumps(report)
    assert "ED25519_VERIFIER_NOT_READY" in report["configuration_issues"]


def test_incomplete_probe_is_not_pass():
    result = evaluate({}, task_id=10, now=NOW)
    assert result["probe"]["status"] == "INCOMPLETE_INPUT"
