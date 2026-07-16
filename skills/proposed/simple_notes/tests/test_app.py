import asyncio
import importlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def request(app, method, path, payload=None):
    sent = []
    body = b"" if payload is None else json.dumps(payload).encode()
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path, "headers": []}
    asyncio.run(app(scope, receive, send))
    status = sent[0]["status"]
    response_body = b"".join(item.get("body", b"") for item in sent[1:])
    return status, json.loads(response_body) if response_body else None


def load_app(monkeypatch, cache_dir):
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(cache_dir))
    import app
    return importlib.reload(app)


def test_create_persists_across_app_reload_and_delete_is_targeted(tmp_path, monkeypatch):
    module = load_app(monkeypatch, tmp_path / "runtime-cache")
    status, created = request(module.app, "POST", "/api/notes", {"content": "First note"})
    assert status == 201
    first_id = created["note"]["id"]
    _, second = request(module.app, "POST", "/api/notes", {"content": "Second note"})

    module = importlib.reload(module)
    status, listing = request(module.app, "GET", "/api/notes")
    assert status == 200
    assert [note["content"] for note in listing["notes"]] == ["Second note", "First note"]

    status, _ = request(module.app, "DELETE", f"/api/notes/{first_id}")
    assert status == 200
    _, listing = request(module.app, "GET", "/api/notes")
    assert [note["id"] for note in listing["notes"]] == [second["note"]["id"]
    ]
    assert (tmp_path / "runtime-cache" / "notes.json").exists()
    assert not (ROOT / "cache" / "notes.json").exists()


def test_malformed_and_missing_requests_are_controlled(tmp_path, monkeypatch):
    module = load_app(monkeypatch, tmp_path / "cache")
    assert request(module.app, "POST", "/api/notes", {"content": "   "})[0] == 400
    assert request(module.app, "DELETE", "/api/notes/not-a-uuid")[0] == 400
    assert request(module.app, "DELETE", "/api/notes/00000000-0000-0000-0000-000000000000")[0] == 404
    assert request(module.app, "PATCH", "/api/notes")[0] == 405
    assert request(module.app, "GET", "/missing")[0] == 404
    assert request(module.app, "GET", "/api/notes")[1] == {"notes": []}


def test_serves_self_contained_application(tmp_path, monkeypatch):
    module = load_app(monkeypatch, tmp_path / "cache")

    async def get_page():
        sent = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        await module.app({"type": "http", "method": "GET", "path": "/", "headers": []}, receive, send)
        return sent

    sent = asyncio.run(get_page())
    assert sent[0]["status"] == 200
    assert b"Simple Notes" in sent[1]["body"]
