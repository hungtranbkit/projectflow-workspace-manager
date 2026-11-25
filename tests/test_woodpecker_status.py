"""Offline Woodpecker status tests: no live server or secrets."""
import io
import json
import urllib.error

from scripts.woodpecker_status import query_status

SHA = "f" * 40
OTHER = "e" * 40


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def fake_open(states, requests):
    def opener(req, timeout):
        requests.append((req.full_url, req.get_header("Authorization"), timeout))
        if "/lookup/" in req.full_url:
            value = {"id": 9, "active": True}
        elif "/pipelines?" in req.full_url:
            value = states
        else:
            raise AssertionError("unexpected endpoint")
        return Response(json.dumps(value).encode())
    return opener


def test_missing_config_no_network():
    calls = []
    out = query_status("test/repo", SHA, "", "", lambda *a, **kw: calls.append(True))
    assert out["ci_status"] == "UNKNOWN" and out["reason"] == "NOT_CONNECTED" and not calls


def test_missing_sha_never_pass():
    out = query_status("test/repo", None, "https://ci.example.com", "secret", fake_open([{"commit": SHA, "number": 2, "status": "success"}], []))
    assert out["ci_status"] == "UNKNOWN" and out["reason"] == "COMMIT_SHA_REQUIRED"


def test_exact_sha_only_success():
    calls = []
    entries = [
        {"commit": OTHER, "number": 19, "status": "success"},
        {"commit": SHA, "number": 20, "status": "failure"},
        {"commit": SHA.upper(), "number": 21, "status": "success", "created": 1000},
    ]
    out = query_status("test/repo", SHA, "https://ci.example.com", "secret", fake_open(entries, calls))
    assert out["ci_status"] == "PASS" and out["pipeline_number"] == 21
    assert calls[0][0].endswith("/api/repos/lookup/test/repo")
    assert all(h == "Bearer secret" for _, h, _ in calls)


def test_stale_sha_never_pass():
    out = query_status("test/repo", SHA, "https://ci.example.com", "secret", fake_open([{"commit": OTHER, "number": 30, "status": "success"}], []))
    assert out["reason"] == "COMMIT_NOT_OBSERVED"


def test_state_mapping():
    for remote, local in [("pending", "QUEUED"), ("blocked", "QUEUED"), ("running", "RUNNING"), ("failure", "FAIL"), ("error", "FAIL"), ("skipped", "UNKNOWN"), ("success", "PASS")]:
        out = query_status("test/repo", SHA, "https://ci.example.com", "secret", fake_open([{"commit": SHA, "number": 1, "status": remote}], []))
        assert out["ci_status"] == local


def test_insecure_url_blocked():
    calls = []
    out = query_status("test/repo", SHA, "http://external.example.com", "secret", lambda *a, **kw: calls.append(True))
    assert out["reason"] == "INSECURE_OR_INVALID_URL" and not calls


def test_inactive_repo():
    def inactive(req, timeout):
        return Response(b'{"id":99,"active":false}')
    out = query_status("test/repo", SHA, "https://ci.example.com", "secret", inactive)
    assert out["reason"] == "REPOSITORY_NOT_ACTIVE"


def test_secret_never_exposed_in_error():
    secret = "CONFIDENTIAL_TOKEN"
    def failing(req, timeout):
        raise urllib.error.URLError("leak? " + secret)
    out = query_status("test/repo", SHA, "https://ci.example.com", secret, failing)
    assert out["ci_status"] == "UNKNOWN" and secret not in json.dumps(out)
