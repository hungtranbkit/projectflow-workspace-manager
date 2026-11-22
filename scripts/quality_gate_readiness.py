#!/usr/bin/env python3
"""Read-only Quality Gate operator/monitor diagnostics. NOT a merge authorization.

The only authorization mechanism is quality_merge_blocker(), called again by
ProjectFlow on the actual merge boundary. This utility cannot sign, merge,
deploy, upload receipts or alter feature flags.

Usage:
  python scripts/quality_gate_readiness.py
  python scripts/quality_gate_readiness.py --task-id 1 --repository-id 1 \
      --repository-name projectflow-workspace-manager --commit <exact_sha>

Environment is inherited from the running verifier, never accepted from a
web request. Output is intentionally safe to put into an observability log:
private keys, HMAC secrets, signatures and filesystem locations are omitted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys

# Direct CLI invocation from any cwd must load this checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.quality_attestation_pilot import (  # noqa: E402
    MAX_AGE, quality_merge_blocker,
)

SHA256_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _safe_receipt_metadata(directory: str, task_id: int, repository_id: int,
                           now: datetime, approved_image_id: str) -> dict:
    """Metadata only; never emit raw receipt, signature or claimed identity."""
    result = {"present": False, "fresh": False, "age_hours": None,
              "expires_at_utc": None, "isolation_claim": None,
              "image_claim_matches_pin": None}
    if not directory:
        return result
    path = Path(directory).expanduser() / f"task-{task_id}-repo-{repository_id}.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return result
        doc = json.loads(path.read_text(encoding="utf-8"))
        result["present"] = True
        claim = doc.get("payload")
        if not isinstance(claim, dict):
            return result
        raw_issued = claim.get("verified_at_utc")
        issued = datetime.fromisoformat(raw_issued)
        if issued.utcoffset() is None:
            return result
        age = now.astimezone(timezone.utc) - issued.astimezone(timezone.utc)
        result["fresh"] = timedelta(seconds=-30) <= age <= MAX_AGE
        result["age_hours"] = round(age.total_seconds() / 3600, 2)
        result["expires_at_utc"] = (issued.astimezone(timezone.utc) + MAX_AGE).isoformat()
        result["isolation_claim"] = (
            "DOCKER_READ_ONLY_NO_NETWORK" if claim.get("qa_isolation") == "DOCKER_READ_ONLY_NO_NETWORK"
            else "OTHER_OR_MISSING"
        )
        result["image_claim_matches_pin"] = (
            claim.get("qa_image_id") == approved_image_id if approved_image_id else None
        )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        # Do not log untrusted receipt content or private path details.
        return result
    return result


def evaluate(environ: dict[str, str], *, task_id: int | None = None,
             repository_id: int | None = None, repository_name: str | None = None,
             expected_sha: str | None = None,
             now: datetime | None = None) -> dict:
    """Safe, non-mutating view of the exact runtime verifier configuration."""
    now = now or datetime.now(timezone.utc)
    raw_ids = environ.get("PROJECTFLOW_QUALITY_PILOT_TASK_IDS", "").strip()
    ids = [x.strip() for x in raw_ids.split(",")] if raw_ids else []
    id_list_valid = all(x.isdecimal() and int(x) > 0 for x in ids) and len(ids) == len(set(ids))
    strict = environ.get("PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA", "").strip() == "1"
    approved = environ.get("PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID", "").strip()
    pub_path = environ.get("PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE", "").strip()
    hmac_present = bool(environ.get("PROJECTFLOW_QUALITY_ATTESTATION_KEY"))
    keyfile_present = bool(environ.get("PROJECTFLOW_QUALITY_ATTESTATION_PRIVATE_KEY_FILE"))
    receipt_dir = environ.get("PROJECTFLOW_QUALITY_ATTESTATION_DIR", "").strip()
    public_key_ok = False
    if pub_path:
        try:
            f = Path(pub_path).expanduser()
            public_key_ok = not f.is_symlink() and f.is_file() and f.stat().st_size <= 4096
        except OSError:
            pass
    receipt_dir_ok = False
    if receipt_dir:
        try:
            p = Path(receipt_dir).expanduser()
            receipt_dir_ok = p.is_dir() and not p.is_symlink()
        except OSError:
            pass
    mode = ("CONFLICT" if hmac_present and pub_path else
            "ED25519_PUBLIC" if pub_path else "HMAC" if hmac_present else "UNCONFIGURED")
    selected = bool(raw_ids)
    config_issues = []
    if selected:
        if not id_list_valid:
            config_issues.append("INVALID_TASK_SELECTION")
        if not environ.get("PROJECTFLOW_QUALITY_PILOT_REPO", "").strip():
            config_issues.append("MISSING_REPOSITORY_SCOPE")
        if not receipt_dir_ok:
            config_issues.append("MISSING_RECEIPT_DIRECTORY")
        if mode != "ED25519_PUBLIC" or not public_key_ok:
            config_issues.append("ED25519_VERIFIER_NOT_READY")
        if keyfile_present:
            config_issues.append("PRIVATE_SIGNING_KEY_IN_VERIFIER_ENVIRONMENT")
        if strict and not SHA256_IMAGE.fullmatch(approved):
            config_issues.append("VERIFIER_APPROVED_IMAGE_NOT_PINNED")
        if not strict:
            config_issues.append("ISOLATED_QA_NOT_REQUIRED")
    snapshot = {
        "schema_version": 1, "type": "DIAGNOSTIC_ONLY_NOT_MERGE_AUTHORIZATION",
        "enabled": selected, "strict_isolation_required": strict,
        "selected_task_ids": [int(x) for x in ids] if id_list_valid else [],
        "repository_scope_configured": bool(environ.get("PROJECTFLOW_QUALITY_PILOT_REPO", "").strip()),
        "signing_verification_mode": mode, "public_key_available": public_key_ok,
        "receipt_directory_available": receipt_dir_ok,
        "approved_immutable_qa_image": bool(SHA256_IMAGE.fullmatch(approved)),
        "signing_private_key_in_verifier_environment": keyfile_present,
        "configuration_issues": config_issues,
        # OS file permissions and an app-side signature cannot establish
        # separate QA identity, GitHub admin controls, or operator approval.
        "independent_qa_principal_proven": False,
        "github_required_merge_check_proven": False,
        "production_end_to_end_accepted": False,
        "status": "DISABLED" if not selected else "CONFIGURATION_BLOCKED" if config_issues else "CONFIGURED_NEEDS_EXTERNAL_ACCEPTANCE",
    }
    probe_fields = (task_id, repository_id, repository_name, expected_sha)
    if any(x is not None for x in probe_fields):
        if not (isinstance(task_id, int) and task_id > 0 and
                isinstance(repository_id, int) and repository_id > 0 and
                isinstance(repository_name, str) and repository_name and
                isinstance(expected_sha, str) and expected_sha):
            snapshot["probe"] = {"status": "INCOMPLETE_INPUT", "receipt": None}
        elif not selected or not id_list_valid or task_id not in snapshot["selected_task_ids"] or (
                environ.get("PROJECTFLOW_QUALITY_PILOT_REPO", "").strip() != repository_name):
            snapshot["probe"] = {"status": "OUT_OF_SCOPE_NOT_PROTECTED", "receipt": None}
        else:
            result = quality_merge_blocker(task_id, repository_id, repository_name,
                                           expected_sha, environ=environ, now=now)
            snapshot["probe"] = {
                "status": "VERIFIER_ACCEPTS_GIVEN_INPUT" if result is None else "VERIFIER_BLOCKS_GIVEN_INPUT",
                "blocker": result,
                # A successful probe is NOT a live merge, and does not
                # authenticate the independent source of the signing key.
                "live_merge_authorized": False,
                "receipt": _safe_receipt_metadata(receipt_dir, task_id, repository_id, now, approved),
            }
    return snapshot


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task-id", type=int)
    p.add_argument("--repository-id", type=int)
    p.add_argument("--repository-name")
    p.add_argument("--commit", dest="expected_sha")
    args = p.parse_args()
    report = evaluate(dict(os.environ), task_id=args.task_id,
                      repository_id=args.repository_id, repository_name=args.repository_name,
                      expected_sha=args.expected_sha)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0  # Monitoring state is data; only a real merge gate can authorize.


if __name__ == "__main__":
    raise SystemExit(main())
