"""Persistent Counter ASGI application.

The application has no third-party runtime dependencies and stores its only
mutable state in the skill-local cache directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
CACHE_DIR = Path(os.environ.get("PERSONAL_AGENT_SKILL_CACHE_DIR", "cache"))
STATE_FILE = CACHE_DIR / "counter.json"
_state_lock = threading.Lock()


def _read_count() -> int:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        count = data.get("count")
        if type(count) is int and count >= 0:
            return count
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return 0


def _write_count(count: int) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"count": count}), encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def _increment_count() -> int:
    with _state_lock:
        count = _read_count() + 1
        _write_count(count)
        return count


async def _request_body(receive: Any) -> bytes:
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        if message["type"] != "http.request":
            continue
        body.extend(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return bytes(body)


async def _respond(send: Any, status: int, body: bytes, content_type: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Serve the counter UI and its minimal JSON API."""
    if scope["type"] != "http":
        return

    method = scope.get("method", "GET").upper()
    path = scope.get("path", "/")

    if method == "GET" and path == "/":
        await _respond(send, 200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        return
    if method == "GET" and path == "/static/styles.css":
        await _respond(send, 200, (STATIC_DIR / "styles.css").read_bytes(), "text/css; charset=utf-8")
        return
    if method == "GET" and path == "/static/app.js":
        await _respond(send, 200, (STATIC_DIR / "app.js").read_bytes(), "text/javascript; charset=utf-8")
        return
    if method == "GET" and path == "/api/count":
        body = json.dumps({"count": await asyncio.to_thread(_read_count)}).encode("utf-8")
        await _respond(send, 200, body, "application/json")
        return
    if method == "POST" and path == "/api/increment":
        await _request_body(receive)
        count = await asyncio.to_thread(_increment_count)
        await _respond(send, 200, json.dumps({"count": count}).encode("utf-8"), "application/json")
        return

    await _respond(send, 404, b'{"error":"Not found"}', "application/json")
