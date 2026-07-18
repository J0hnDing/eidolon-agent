"""Dependency-free ASGI notes application."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


MAX_NOTE_LENGTH = 10_000
MAX_REQUEST_BYTES = 32_000
ASSET_DIR = Path(__file__).resolve().parent / "static"
_storage_lock = threading.RLock()


class StorageError(Exception):
    """A controlled persistence failure."""


def _cache_dir() -> Path:
    configured = os.environ.get("PERSONAL_AGENT_SKILL_CACHE_DIR")
    return Path(configured) if configured else Path.cwd() / "cache"


def _notes_path() -> Path:
    return _cache_dir() / "notes.json"


def _validate_notes(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise StorageError("Saved notes could not be read safely.")
    raw_notes = value.get("notes")
    if not isinstance(raw_notes, list):
        raise StorageError("Saved notes could not be read safely.")
    notes: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_notes:
        if not isinstance(item, dict) or set(item) != {"id", "content", "created_at"}:
            raise StorageError("Saved notes could not be read safely.")
        if not all(isinstance(item[key], str) for key in item):
            raise StorageError("Saved notes could not be read safely.")
        if not item["id"] or item["id"] in seen or not item["content"].strip():
            raise StorageError("Saved notes could not be read safely.")
        try:
            datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise StorageError("Saved notes could not be read safely.") from exc
        seen.add(item["id"])
        notes.append({key: item[key] for key in ("id", "content", "created_at")})
    return sorted(notes, key=lambda note: (note["created_at"], note["id"]))


def _load_notes() -> list[dict[str, str]]:
    path = _notes_path()
    if not path.exists():
        return []
    try:
        return _validate_notes(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError("Saved notes could not be read safely.") from exc


def _save_notes(notes: list[dict[str, str]]) -> None:
    directory = _cache_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "notes": notes}, ensure_ascii=False, indent=2)
        descriptor, temporary_name = tempfile.mkstemp(prefix="notes-", suffix=".tmp", dir=directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, _notes_path())
        finally:
            if temporary.exists():
                temporary.unlink()
    except OSError as exc:
        raise StorageError("Notes could not be saved.") from exc


async def _body(receive: Any) -> bytes:
    data = bytearray()
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            continue
        data.extend(message.get("body", b""))
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("Request is too large.")
        more = message.get("more_body", False)
    return bytes(data)


async def _respond(send: Any, status: int, body: bytes, content_type: str) -> None:
    headers = [(b"content-type", content_type.encode()), (b"cache-control", b"no-store")]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _json(send: Any, status: int, payload: Any) -> None:
    await _respond(send, status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")


def _error(message: str) -> dict[str, str]:
    return {"error": message}


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

    method, path = scope["method"], scope.get("path", "/")
    try:
        if method == "GET" and path in {"/", "/index.html"}:
            await _respond(send, 200, (ASSET_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if method == "GET" and path in {"/app.css", "/app.js"}:
            kind = "text/css; charset=utf-8" if path.endswith("css") else "text/javascript; charset=utf-8"
            await _respond(send, 200, (ASSET_DIR / path[1:]).read_bytes(), kind)
            return
        if method == "GET" and path == "/api/notes":
            notes = await asyncio.to_thread(_locked_load)
            await _json(send, 200, {"notes": notes})
            return
        if method == "POST" and path == "/api/notes":
            raw = await _body(receive)
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                await _json(send, 400, _error("Please send a valid note."))
                return
            if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
                await _json(send, 400, _error("Note content is required."))
                return
            content = payload["content"].strip()
            if not content:
                await _json(send, 422, _error("Write something before saving your note."))
                return
            if len(content) > MAX_NOTE_LENGTH:
                await _json(send, 422, _error(f"Notes can be up to {MAX_NOTE_LENGTH:,} characters."))
                return
            note = await asyncio.to_thread(_locked_create, content)
            await _json(send, 201, {"note": note})
            return
        if method == "DELETE" and path.startswith("/api/notes/"):
            note_id = path.removeprefix("/api/notes/")
            if not note_id or "/" in note_id:
                await _json(send, 404, _error("That note was not found."))
                return
            deleted = await asyncio.to_thread(_locked_delete, note_id)
            if not deleted:
                await _json(send, 404, _error("That note was not found."))
                return
            await _json(send, 200, {"deleted": note_id})
            return
        await _json(send, 404, _error("This page was not found."))
    except ValueError as exc:
        await _json(send, 413, _error(str(exc)))
    except (StorageError, OSError):
        await _json(send, 500, _error("Notes are temporarily unavailable. Your existing notes were not changed."))


def _locked_load() -> list[dict[str, str]]:
    with _storage_lock:
        return _load_notes()


def _locked_create(content: str) -> dict[str, str]:
    with _storage_lock:
        notes = _load_notes()
        note = {
            "id": uuid4().hex,
            "content": content,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        notes.append(note)
        _save_notes(sorted(notes, key=lambda item: (item["created_at"], item["id"])))
        return note


def _locked_delete(note_id: str) -> bool:
    with _storage_lock:
        notes = _load_notes()
        remaining = [note for note in notes if note["id"] != note_id]
        if len(remaining) == len(notes):
            return False
        _save_notes(remaining)
        return True
