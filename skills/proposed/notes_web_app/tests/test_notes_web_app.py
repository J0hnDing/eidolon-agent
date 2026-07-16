from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _asgi_request(
    asgi_app: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], bytes]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    messages = [
        {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await asgi_app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        },
        receive,
        send,
    )

    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], dict(start["headers"]), response_body


def _request(
    asgi_app: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    status, _headers, body = asyncio.run(_asgi_request(asgi_app, method, path, payload))
    return status, json.loads(body.decode("utf-8"))


def test_crud_routes_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(tmp_path))
    module = importlib.import_module("app")

    status, payload = _request(module.app, "GET", "/api/notes")
    assert status == 200
    assert payload == {"notes": []}

    status, payload = _request(
        module.app,
        "POST",
        "/api/notes",
        {"title": "First note", "body": "Original body"},
    )
    assert status == 201
    note = payload["note"]
    assert note["id"] == 1
    assert note["title"] == "First note"
    assert note["body"] == "Original body"
    assert note["created_at"]
    assert note["updated_at"]

    status, payload = _request(module.app, "GET", f"/api/notes/{note['id']}")
    assert status == 200
    assert payload["note"]["title"] == "First note"

    status, payload = _request(
        module.app,
        "PUT",
        f"/api/notes/{note['id']}",
        {"title": "Updated note", "body": "Updated body"},
    )
    assert status == 200
    assert payload["note"]["title"] == "Updated note"
    assert payload["note"]["body"] == "Updated body"
    assert payload["note"]["created_at"] == note["created_at"]
    assert payload["note"]["updated_at"]

    status, payload = _request(module.app, "GET", "/api/notes")
    assert status == 200
    assert [listed["title"] for listed in payload["notes"]] == ["Updated note"]

    status, payload = _request(module.app, "DELETE", f"/api/notes/{note['id']}")
    assert status == 200
    assert payload == {"deleted": True}

    status, payload = _request(module.app, "GET", "/api/notes")
    assert status == 200
    assert payload == {"notes": []}


def test_persistence_uses_runtime_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(tmp_path))
    module = importlib.import_module("app")

    status, payload = _request(
        module.app,
        "POST",
        "/api/notes",
        {"title": "Persistent", "body": "Stored in SQLite"},
    )
    assert status == 201
    assert (tmp_path / "notes.sqlite3").exists()

    reloaded = importlib.reload(module)
    status, payload = _request(reloaded.app, "GET", "/api/notes")
    assert status == 200
    assert payload["notes"][0]["title"] == "Persistent"
    assert payload["notes"][0]["body"] == "Stored in SQLite"


def test_static_entrypoint_serves_owned_browser_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(tmp_path))
    module = importlib.import_module("app")

    status, _headers, body = asyncio.run(_asgi_request(module.app, "GET", "/"))
    assert status == 200
    html = body.decode("utf-8")
    assert "<title>Notes</title>" in html
    assert "/app.js" in html

    status, _headers, body = asyncio.run(_asgi_request(module.app, "GET", "/app.js"))
    assert status == 200
    assert b"/api/notes" in body
