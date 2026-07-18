import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
app_module = importlib.import_module("app")


def request(method, path, payload=None):
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
    asyncio.run(app_module.app(scope, receive, send))
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    response_body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, response_body


@pytest.fixture(autouse=True)
def cache_contract(tmp_path, monkeypatch):
    cache = tmp_path / "runtime-cache"
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(cache))
    monkeypatch.chdir(tmp_path)
    return cache


def test_create_list_restart_and_delete(cache_contract):
    first_status, first_body = request("POST", "/api/notes", {"content": "  First <script>alert(1)</script>  "})
    second_status, second_body = request("POST", "/api/notes", {"content": "Second"})
    assert first_status == second_status == 201
    first = json.loads(first_body)["note"]
    second = json.loads(second_body)["note"]
    assert (cache_contract / "notes.json").is_file()

    restarted = importlib.reload(app_module)
    status, body = request("GET", "/api/notes")
    listed = json.loads(body)["notes"]
    assert status == 200
    assert [item["id"] for item in listed] == [first["id"], second["id"]]
    assert listed[0]["content"] == "First <script>alert(1)</script>"
    assert "textContent=item.content" in (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    status, _ = request("DELETE", f"/api/notes/{first['id']}")
    assert status == 200
    assert [item["id"] for item in restarted._load_notes()] == [second["id"]]


def test_validation_missing_id_and_malformed_request():
    assert request("POST", "/api/notes", {"content": "   "})[0] == 422
    assert request("POST", "/api/notes", {"wrong": "shape"})[0] == 400
    assert request("DELETE", "/api/notes/missing")[0] == 404


def test_malformed_storage_is_not_overwritten(cache_contract):
    cache_contract.mkdir()
    storage = cache_contract / "notes.json"
    storage.write_text('{"version": 1, "notes": "broken"}', encoding="utf-8")
    before = storage.read_bytes()
    status, body = request("POST", "/api/notes", {"content": "Do not save"})
    assert status == 500
    assert b"temporarily unavailable" in body
    assert storage.read_bytes() == before


def test_serves_package_assets_when_working_directory_changes():
    status, body = request("GET", "/")
    assert status == 200
    assert b"Simple Notes" in body
