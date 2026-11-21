"""Opt-in, fail-closed, SHA-pinned independent QA receipt for merge gates.

The ProjectFlow web process NEVER runs agent-authored pytest code to decide
whether to merge. A separate trusted QA runner signs a machine-generated
proof; this module only verifies that proof. Disabled by default.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PILOT_POLICY_TASK = "PROJECTFLOW-MONITOR-REPORT-PILOT"
PILOT_TEST = "tests/test_harness_monitor_report.py"
MAX_AGE = timedelta(hours=24)
SHA_RE = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?\Z")
DIGEST_RE = re.compile(r"[a-f0-9]{64}\Z")


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sign_receipt(payload: dict, key: str) -> dict:
    """Only the independent QA process should ever call this with its key."""
    if not isinstance(key, str) or len(key.encode("utf-8")) < 32:
        raise ValueError("Quality attestation signing key must be at least 32 bytes")
    return {
        "payload": payload,
        "hmac_sha256": hmac.new(key.encode("utf-8"), _canonical(payload), hashlib.sha256).hexdigest(),
    }


def verified_receipt(receipt: dict, key: str, *, task_id: int, repository_id: int,
                     repository_name: str, expected_sha: str,
                     now: datetime | None = None) -> str | None:
    """Return None on exact valid proof, otherwise a stable failure code."""
    try:
        if not isinstance(receipt, dict) or set(receipt) != {"payload", "hmac_sha256"}:
            return "QUALITY_ATTESTATION_INVALID"
        payload = receipt["payload"]
        signature = receipt["hmac_sha256"]
        if not isinstance(payload, dict) or not isinstance(signature, str) or not DIGEST_RE.fullmatch(signature):
            return "QUALITY_ATTESTATION_INVALID"
        provided = sign_receipt(payload, key)["hmac_sha256"]
        if not hmac.compare_digest(signature, provided):
            return "QUALITY_ATTESTATION_INVALID"
        return _validate_payload(payload, task_id=task_id, repository_id=repository_id,
                                 repository_name=repository_name, expected_sha=expected_sha, now=now)
    except (ValueError, TypeError, KeyError):
        return "QUALITY_ATTESTATION_INVALID"


def _validate_payload(payload: dict, *, task_id: int, repository_id: int,
                      repository_name: str, expected_sha: str, now: datetime | None) -> str | None:
    """Shared semantic check: both HMAC and Ed25519 bind the same exact task and SHA."""
    try:
        if (payload.get("schema_version") != 1 or
            payload.get("projectflow_task_id") != task_id or
            payload.get("repository_id") != repository_id or
            payload.get("repository_name") != repository_name or
            payload.get("git_head") != expected_sha or
            payload.get("policy_task_id") != PILOT_POLICY_TASK):
            return "QUALITY_ATTESTATION_STALE"
        if (not isinstance(expected_sha, str) or not SHA_RE.fullmatch(expected_sha) or
            payload.get("status") != "PASS" or
            payload.get("test_exit_code") != 0 or
            payload.get("lint_violations") != [] or
            payload.get("scope_dirty") is not False or
            payload.get("test_files") != [PILOT_TEST] or
            not isinstance(payload.get("code_sha256"), dict) or
            not payload["code_sha256"] or
            not isinstance(payload.get("test_sha256"), dict) or
            set(payload["test_sha256"]) != {PILOT_TEST} or
            any(not isinstance(v, str) or not DIGEST_RE.fullmatch(v)
                for v in [payload.get("policy_sha256"),
                          *payload["code_sha256"].values(),
                          *payload["test_sha256"].values()])):
            return "QUALITY_ATTESTATION_INVALID"
        checked_at = datetime.fromisoformat(payload["verified_at_utc"])
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            return "QUALITY_ATTESTATION_INVALID"
        current = now or datetime.now(timezone.utc)
        age = current.astimezone(timezone.utc) - checked_at.astimezone(timezone.utc)
        if age < timedelta(seconds=-30) or age > MAX_AGE:
            return "QUALITY_ATTESTATION_STALE"
    except (ValueError, TypeError, KeyError, OverflowError):
        return "QUALITY_ATTESTATION_INVALID"
    return None



def sign_ed25519_receipt(payload: dict, private_key_pem: bytes) -> dict:
    """Sign with an asymmetric private key held only by the independent QA host."""
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Expected an Ed25519 private signing key")
    signature = private_key.sign(_canonical(payload))
    return {
        "payload": payload, "signature_algorithm": "Ed25519",
        "ed25519_signature": base64.b64encode(signature).decode("ascii"),
    }


def verified_ed25519_receipt(receipt: dict, public_key_pem: bytes, *,
                             task_id: int, repository_id: int,
                             repository_name: str, expected_sha: str,
                             now: datetime | None = None) -> str | None:
    """Verify a detached signature; the verifier never holds the private key."""
    try:
        if (not isinstance(receipt, dict) or
            set(receipt) != {"payload", "signature_algorithm", "ed25519_signature"} or
            receipt.get("signature_algorithm") != "Ed25519" or
            not isinstance(receipt.get("payload"), dict)):
            return "QUALITY_ATTESTATION_INVALID"
        signature_text = receipt.get("ed25519_signature")
        if not isinstance(signature_text, str):
            return "QUALITY_ATTESTATION_INVALID"
        signature = base64.b64decode(signature_text, validate=True)
        if len(signature) != 64:
            return "QUALITY_ATTESTATION_INVALID"
        public_key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(public_key, Ed25519PublicKey):
            return "QUALITY_ATTESTATION_CONFIG"
        public_key.verify(signature, _canonical(receipt["payload"]))
    except (ValueError, TypeError, InvalidSignature, OverflowError):
        return "QUALITY_ATTESTATION_INVALID"
    return _validate_payload(receipt["payload"], task_id=task_id, repository_id=repository_id,
                             repository_name=repository_name, expected_sha=expected_sha, now=now)


def quality_merge_blocker(task_id: int, repository_id: int, repository_name: str,
                          expected_sha: str | None, *,
                          environ: dict | None = None,
                          now: datetime | None = None) -> str | None:
    """One policy entry point for both GitHub merges and local integrations.

    No state changes and no host commands. An unset feature flag means
    this experiment has zero impact on existing ProjectFlow tasks.
    """
    env = os.environ if environ is None else environ
    raw_ids = env.get("PROJECTFLOW_QUALITY_PILOT_TASK_IDS", "").strip()
    if not raw_ids:
        return None
    ids = [x.strip() for x in raw_ids.split(",")]
    if any(not x.isdecimal() for x in ids):
        return "QUALITY_ATTESTATION_CONFIG"
    if str(task_id) not in ids:
        return None
    selected_repo = env.get("PROJECTFLOW_QUALITY_PILOT_REPO", "").strip()
    if not selected_repo:
        return "QUALITY_ATTESTATION_CONFIG"
    if selected_repo != repository_name:
        return None
    key = env.get("PROJECTFLOW_QUALITY_ATTESTATION_KEY")
    public_key_path = env.get("PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE", "").strip()
    state_dir = env.get("PROJECTFLOW_QUALITY_ATTESTATION_DIR")
    if not state_dir or (bool(key) == bool(public_key_path)):
        # Exactly one verification method. No silent fallback if public-key mode fails.
        return "QUALITY_ATTESTATION_CONFIG"
    if not public_key_path and (not isinstance(key, str) or len(key.encode("utf-8")) < 32):
        return "QUALITY_ATTESTATION_CONFIG"
    if not expected_sha or not SHA_RE.fullmatch(expected_sha):
        return "QUALITY_ATTESTATION_STALE"
    directory = Path(state_dir).expanduser().resolve()
    path = directory / f"task-{task_id}-repo-{repository_id}.json"
    try:
        # Never accept a path selected by a task, agent, or browser.
        # A symlinked file could point into an agent-writable workspace.
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return "QUALITY_ATTESTATION_MISSING"
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "QUALITY_ATTESTATION_MISSING"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "QUALITY_ATTESTATION_INVALID"
    if env.get("PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA", "").strip() == "1":
        proof = receipt.get("payload") if isinstance(receipt, dict) else None
        if (not isinstance(proof, dict) or
            proof.get("qa_isolation") != "DOCKER_READ_ONLY_NO_NETWORK" or
            not isinstance(proof.get("qa_image_id"), str) or
            not re.fullmatch(r"sha256:[a-f0-9]{64}", proof["qa_image_id"])):
            return "QUALITY_ATTESTATION_INVALID"
    if public_key_path:
        pubfile = Path(public_key_path).expanduser()
        try:
            if pubfile.is_symlink() or not pubfile.is_file() or pubfile.stat().st_size > 4096:
                return "QUALITY_ATTESTATION_CONFIG"
            public_pem = pubfile.read_bytes()
        except OSError:
            return "QUALITY_ATTESTATION_CONFIG"
        try:
            return verified_ed25519_receipt(receipt, public_pem,
                                            task_id=task_id, repository_id=repository_id,
                                            repository_name=repository_name,
                                            expected_sha=expected_sha, now=now)
        except (ValueError, TypeError):
            return "QUALITY_ATTESTATION_INVALID"
    return verified_receipt(receipt, key, task_id=task_id, repository_id=repository_id,
                            repository_name=repository_name, expected_sha=expected_sha,
                            now=now)


def payload_from_machine_report(report: dict, *, task_id: int, repository_id: int,
                                repository_name: str, now: datetime | None = None) -> dict:
    """Only serialize a real, clean, machine-produced PASS into an attestable payload."""
    if (report.get("status") != "PASS" or report.get("test_exit_code") != 0 or
        report.get("scope_dirty") is not False or report.get("lint_violations") != [] or
        report.get("test_command", [])[-1:] != [PILOT_TEST] or
        not SHA_RE.fullmatch(report.get("git_head") or "")):
        raise ValueError("Cannot sign a failed, dirty, or unpinned QA run")
    return {
        "schema_version": 1,
        "projectflow_task_id": task_id,
        "repository_id": repository_id,
        "repository_name": repository_name,
        "policy_task_id": PILOT_POLICY_TASK,
        "git_head": report["git_head"],
        "verified_at_utc": (now or datetime.now(timezone.utc)).isoformat(),
        "status": report["status"],
        "test_exit_code": report["test_exit_code"],
        "lint_violations": report["lint_violations"],
        "scope_dirty": report["scope_dirty"],
        "test_files": [PILOT_TEST],
        "code_sha256": report["code_sha256"],
        "test_sha256": report["test_sha256"],
        "policy_sha256": report["policy_sha256"],
        "qa_isolation": report.get("qa_isolation", "HOST_DIRECT"),
        "qa_image_id": report.get("qa_image_id"),
    }
