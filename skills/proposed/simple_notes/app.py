"""Self-contained ASGI notes application with local JSON persistence."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _cache_dir() -> Path:
    configured = os.environ.get("PERSONAL_AGENT_SKILL_CACHE_DIR")
    return Path(configured) if configured else Path.cwd() / "cache"


def _notes_path() -> Path:
    return _cache_dir() / "notes.json"


def _read_notes() -> list[dict[str, str]]:
    path = _notes_path()
    with _LOCK:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]


def _write_notes(notes: list[dict[str, str]]) -> None:
    path = _notes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        handle, temporary_name = tempfile.mkstemp(
            prefix="notes-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as temporary:
                json.dump(notes, temporary, ensure_ascii=False, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def _create_note(content: str) -> dict[str, str]:
    note = {
        "id": str(uuid.uuid4()),
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _LOCK:
        notes = _read_notes()
        notes.append(note)
        _write_notes(notes)
    return note


def _delete_note(note_id: str) -> bool:
    with _LOCK:
        notes = _read_notes()
        remaining = [note for note in notes if note.get("id") != note_id]
        if len(remaining) == len(notes):
            return False
        _write_notes(remaining)
        return True


async def _body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


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


async def _json(send: Any, status: int, payload: Any) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await _respond(send, status, encoded, "application/json; charset=utf-8")


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        
    if scope["type"] != "http":
        return

    method = scope.get("method", "GET").upper()
    path = scope.get("path", "/")

    if method == "GET" and path in {"/", "/index.html"}:
        await _respond(send, 200, (_STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        return
    if method == "GET" and path == "/styles.css":
        await _respond(send, 200, (_STATIC_DIR / "styles.css").read_bytes(), "text/css; charset=utf-8")
        return
    if method == "GET" and path == "/app.js":
        await _respond(send, 200, (_STATIC_DIR / "app.js").read_bytes(), "text/javascript; charset=utf-8")
        return
    if path == "/api/notes" and method == "GET":
        await _json(send, 200, {"notes": list(reversed(_read_notes()))})
        return
    if path == "/api/notes" and method == "POST":
        try:
            payload = json.loads((await _body(receive)).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            await _json(send, 400, {"error": "Request body must be valid JSON."})
            return
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str) or not content.strip():
            await _json(send, 400, {"error": "Note content is required."})
            return
        if len(content.strip()) > 10_000:
            await _json(send, 400, {"error": "Note content must be 10,000 characters or fewer."})
            return
        await _json(send, 201, {"note": _create_note(content.strip())})
        return
    if path.startswith("/api/notes/") and method == "DELETE":
        note_id = path.removeprefix("/api/notes/")
        try:
            uuid.UUID(note_id)
        except ValueError:
            await _json(send, 400, {"error": "Invalid note identifier."})
            return
        if not _delete_note(note_id):
            await _json(send, 404, {"error": "Note not found."})
            return
        await _json(send, 200, {"deleted": note_id})
        return
    if path.startswith("/api/notes"):
        await _json(send, 405, {"error": "Method not allowed."})
        return
    await _json(send, 404, {"error": "Not found."})

