#!/usr/bin/env python3
"""Read-only, fail-closed Woodpecker CI status for ProjectFlow monitors.

Never uses a write API or treats a different commit as passing.
Reads WOODPECKER_URL and WOODPECKER_TOKEN from process environment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _get_json(base: str, path: str, token: str, opener: Callable = urlopen) -> Any:
    url = f"{base.rstrip('/')}/api/{path.lstrip('/')}"
    req = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with opener(req, timeout=7) as response:
        return json.load(response)


def _valid_url(base: str) -> bool:
    parsed = urlsplit(base)
    return bool(parsed.hostname) and not (parsed.username or parsed.password) and (
        parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))
    ) and not parsed.query and not parsed.fragment and parsed.path in ("", "/")


def _base(repo: str, expected_sha: str | None) -> dict[str, Any]:
    return {
        "source": "woodpecker",
        "repo": repo,
        "expected_sha": expected_sha,
        "ci_status": "UNKNOWN",
        "pipeline_status": None,
        "pipeline_sha": None,
        "pipeline_number": None,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def query_status(repo: str, expected_sha: str | None, url: str, token: str, opener: Callable = urlopen) -> dict[str, Any]:
    """Return a bounded read-only JSON record. Only exact SHA + success is PASS."""
    result = _base(repo, expected_sha)
    if not REPO_RE.fullmatch(repo):
        result["reason"] = "INVALID_REPOSITORY"
        return result
    if expected_sha is not None and not SHA_RE.fullmatch(expected_sha):
        result["reason"] = "INVALID_COMMIT_SHA"
        return result
    if not url or not token:
        result["reason"] = "NOT_CONNECTED"
        return result
    if not _valid_url(url):
        result["reason"] = "INSECURE_OR_INVALID_URL"
        return result
    try:
        name = "/".join(quote(seg, safe="") for seg in repo.split("/"))
        metadata = _get_json(url, f"repos/lookup/{name}", token, opener)
        repo_id = metadata.get("id") if isinstance(metadata, dict) else None
        if type(repo_id) is not int or repo_id <= 0 or not metadata.get("active", False):
            result["reason"] = "REPOSITORY_NOT_ACTIVE"
            return result
        entries = _get_json(url, f"repos/{repo_id}/pipelines?page=1&perPage=50", token, opener)
        if not isinstance(entries, list):
            result["reason"] = "INVALID_API_RESPONSE"
            return result
        if expected_sha is None:
            result["reason"] = "COMMIT_SHA_REQUIRED"
            return result
        matches = [p for p in entries if isinstance(p, dict) and str(p.get("commit", "")).lower() == expected_sha.lower()]
        if not matches:
            result["reason"] = "COMMIT_NOT_OBSERVED"
            return result
        pipeline = max(matches, key=lambda p: (int(p.get("number") or 0), int(p.get("created") or 0)))
        state = str(pipeline.get("status", "")).lower()
        result.update({
            "pipeline_status": state,
            "pipeline_sha": pipeline.get("commit"),
            "pipeline_number": pipeline.get("number"),
            "pipeline_event": pipeline.get("event"),
            "pipeline_created": pipeline.get("created"),
            "pipeline_finished": pipeline.get("finished"),
        })
        if state == "success":
            result["ci_status"] = "PASS"
        elif state in ("failure", "error"):
            result["ci_status"] = "FAIL"
        elif state == "running":
            result["ci_status"] = "RUNNING"
        elif state in ("pending", "blocked", "waiting", "created", "approved", "queued"):
            result["ci_status"] = "QUEUED"
        else:
            result["reason"] = "NOT_SUCCESS_OR_UNRECOGNIZED_STATE"
        return result
    except HTTPError as exc:
        result["reason"] = "AUTH_OR_API_ERROR" if exc.code in (401, 403) else "API_ERROR"
    except (URLError, TimeoutError, OSError, ValueError, TypeError):
        result["reason"] = "API_UNREACHABLE_OR_INVALID_RESPONSE"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="hungtranbkit/projectflow-workspace-manager")
    parser.add_argument("--sha", default=None, help="Exact full 40-character commit SHA")
    args = parser.parse_args()
    status = query_status(args.repo, args.sha, os.environ.get("WOODPECKER_URL", ""), os.environ.get("WOODPECKER_TOKEN", ""))
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    return 0 if status["ci_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
