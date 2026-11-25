import base64
import hashlib
import json
import time
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.fleet_steward import Memory, SOURCES, VN, read_view
from scripts.fleet_collect import github_project, publish

PROJECT = "hungtranbkit/zalo-group-hub"


def sample(at, version="1", coverage="ok", rows=True):
    sources = dict.fromkeys(SOURCES, "not-configured")
    sources["android"] = coverage
    item = dict(
        source="android",
        name="SM-G935L/app",
        project=PROJECT,
        state="process-present",
        version=version,
        location="SM-G935L",
    )
    return dict(at=at, node="m910", sources=sources, items=[item] if rows else [])


def test_replay_late_sample_and_partial_loss(tmp_path):
    m = Memory(tmp_path / "memory.db")
    m.init()
    now = int(time.time())
    early = sample(now - 80)
    late = sample(now - 40, "2")
    assert m.ingest(late) and m.ingest(early)
    assert not m.ingest(early)
    with pytest.raises(ValueError):
        m.ingest(sample(now - 80, "other"))
    m.ingest(sample(now - 20, coverage="unavailable", rows=False))
    m.ingest(sample(now, "2"))
    view = read_view(Memory(m.path), q="Zalo Hub thay đổi gì?")
    assert view["mode"] == "history" and view["project"] == PROJECT
    assert [(e["event"], e["at"]) for e in view["events"]] == [
        ("Thay đổi", now - 40),
        ("Lần đầu thấy", now - 80),
    ]
    assert read_view(m, q="Zalo Hub đang chạy ở đâu?")["items"][0]["version"] == "2"
    assert read_view(m, q="Zalo Hub tiếp theo cần làm gì?")["actions"]
    assert (
        read_view(m, q="Zalo Hub hôm qua")["when"]
        == (datetime.now(VN).date() - timedelta(days=1)).isoformat()
    )


def test_private_ui_and_signed_transport(tmp_path, monkeypatch):
    m = Memory(tmp_path / "steward.db")
    m.init()
    keys = tmp_path / "keys.json"
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    keys.write_text(json.dumps({"m910": base64.b64encode(public).decode()}))
    monkeypatch.setenv("PROJECTFLOW_STEWARD_KEYS", str(keys))
    monkeypatch.setenv("PROJECTFLOW_STEWARD_DB", str(m.path))
    root = tmp_path / "repo"
    root.mkdir()
    app = create_app(Settings(root, "127.0.0.1", 8765, tmp_path / "pf.db", 10))
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/steward").status_code == 401
    assert "SM-G935L" not in client.get("/steward").text
    report = sample(int(time.time()))
    raw = (tmp_path / publish(report, tmp_path)).read_bytes()
    headers = {
        "X-PF-Node": "m910",
        "X-PF-Signature": base64.b64encode(private.sign(raw)).decode(),
    }
    assert (
        client.post(
            "/api/steward/captures", content=raw + b"x", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/steward/captures",
            content=raw,
            headers={**headers, "X-PF-Node": "nec1"},
        ).status_code
        == 403
    )
    assert (
        client.post("/api/steward/captures", content=raw, headers=headers).json()[
            "sha256"
        ]
        == hashlib.sha256(raw).hexdigest()
    )
    ticket = m.issue()
    assert (
        client.post(
            "/steward/login",
            data={"code": ticket},
            headers={"origin": "https://wrong.test"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/steward/login", data={"code": ticket}, follow_redirects=False
        ).status_code
        == 303
    )
    assert client.post("/steward/login", data={"code": ticket}).status_code == 403
    for intent in ("now", "next", "history"):
        page = client.get("/steward", params={"q": "Zalo Hub", "intent": intent})
        assert (
            page.status_code == 200
            and page.headers["cache-control"] == "private, no-store"
        )
    assert "SM-G935L/app" in client.get("/steward?q=Zalo+Hub").text
    assert (
        client.get("/api/steward?q=Zalo+Hub").json()["items"][0]["name"]
        == "SM-G935L/app"
    )
    client.post("/steward/logout")
    assert client.get("/api/steward").status_code == 401


def test_remote_identity_and_missing_measurement(tmp_path):
    assert (
        github_project("https://credential@github.com/owner/repo.git") == "owner/repo"
    )
    assert github_project("git@github.com:owner/repo.git") == "owner/repo"
    assert not github_project("https://other.test/owner/repo")
    m = Memory(tmp_path / "m")
    m.init()
    assert all(not n["fresh"] and n["at"] is None for n in read_view(m)["nodes"])


def test_questions_find_a_repo_by_short_name(tmp_path):
    memory = Memory(tmp_path / "ops.db")
    memory.init()
    report = sample(int(time.time()))
    report["items"][0]["project"] = "example/quang-cao"
    memory.ingest(report)
    assert (
        read_view(memory, q="Quang Cao chạy ở đâu?")["project"] == "example/quang-cao"
    )
