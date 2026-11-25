import time

from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.services.fleet_steward import Memory, SOURCES


def test_cc95_map_is_private_and_uses_steward_data(tmp_path, monkeypatch):
    memory = Memory(tmp_path / "steward.db")
    memory.init()
    monkeypatch.setenv("PROJECTFLOW_STEWARD_DB", str(memory.path))
    root = tmp_path / "repo"
    root.mkdir()
    app = create_app(Settings(root, "127.0.0.1", 8765, tmp_path / "pf.db", 10))
    client = TestClient(app, base_url="https://testserver")
    assert 'id="cc95-map"' not in client.get("/steward").text
    assert client.get("/api/steward").status_code == 401
    sources = dict.fromkeys(SOURCES, "not-configured")
    sources["git"] = "ok"
    memory.ingest(dict(node="m910", at=int(time.time()), sources=sources, items=[
        dict(source="git", name="sample checkout", project="example/repo", state="observed", version="abc", location="fixture")
    ]))
    assert client.post("/steward/login", data={"code": memory.issue()},
                       headers={"origin": "https://testserver"}, follow_redirects=False).status_code == 303
    page = client.get("/steward", params={"project": "example/repo"})
    assert page.status_code == 200
    assert page.headers["cache-control"] == "private, no-store"
    assert 'id="cc95-map"' in page.text
    assert 'data-project="example/repo"' in page.text
    assert "/static/cc95-map.js" in page.text
    data = client.get("/api/steward?intent=now")
    assert data.status_code == 200
    assert data.headers["cache-control"] == "private, no-store"
    assert data.json()["projects"] == ["example/repo"]
    assert data.json()["items"][0]["name"] == "sample checkout"
    assert 'id="cc95-map"' not in client.get("/steward?intent=history").text
    client.post("/steward/logout")
    assert client.get("/api/steward").status_code == 401
