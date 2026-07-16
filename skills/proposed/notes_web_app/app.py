from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _cache_dir() -> Path:
    configured = os.environ.get("PERSONAL_AGENT_SKILL_CACHE_DIR")
    if configured:
        path = Path(configured)
    else:
        path = Path.cwd() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _db_path() -> Path:
    return _cache_dir() / "notes.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path())
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _row_to_note(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _list_notes() -> list[dict[str, Any]]:
    with closing(_connect()) as connection:
        rows = connection.execute(
            """
            SELECT id, title, body, created_at, updated_at
            FROM notes
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
    return [_row_to_note(row) for row in rows]


def _get_note(note_id: int) -> dict[str, Any] | None:
    with closing(_connect()) as connection:
        row = connection.execute(
            """
            SELECT id, title, body, created_at, updated_at
            FROM notes
            WHERE id = ?
            """,
            (note_id,),
        ).fetchone()
    return _row_to_note(row) if row else None


def _clean_note_payload(payload: dict[str, Any]) -> tuple[str, str]:
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", ""))
    if not title:
        title = "Untitled note"
    return title[:200], body


def _create_note(payload: dict[str, Any]) -> dict[str, Any]:
    title, body = _clean_note_payload(payload)
    timestamp = _now()
    with closing(_connect()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO notes (title, body, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (title, body, timestamp, timestamp),
        )
        connection.commit()
        note_id = int(cursor.lastrowid)
    note = _get_note(note_id)
    if note is None:
        raise RuntimeError("Created note could not be loaded.")
    return note


def _update_note(note_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    if _get_note(note_id) is None:
        return None
    title, body = _clean_note_payload(payload)
    timestamp = _now()
    with closing(_connect()) as connection:
        connection.execute(
            """
            UPDATE notes
            SET title = ?, body = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, body, timestamp, note_id),
        )
        connection.commit()
    return _get_note(note_id)


def _delete_note(note_id: int) -> bool:
    with closing(_connect()) as connection:
        cursor = connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        connection.commit()
    return cursor.rowcount > 0


async def _read_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


async def _send_response(
    send: Any,
    status: int,
    body: bytes,
    content_type: str,
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode("utf-8")),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _json_response(send: Any, status: int, payload: Any) -> None:
    await _send_response(
        send,
        status,
        json.dumps(payload).encode("utf-8"),
        "application/json; charset=utf-8",
    )


def _not_found_payload() -> dict[str, str]:
    return {"error": "Not found"}


def _parse_note_id(path: str) -> int | None:
    prefix = "/api/notes/"
    if not path.startswith(prefix):
        return None
    raw_id = path[len(prefix) :].strip("/")
    if not raw_id.isdigit():
        return None
    return int(raw_id)


async def _handle_api(scope: dict[str, Any], receive: Any, send: Any) -> None:
    method = scope["method"].upper()
    path = unquote(scope.get("path", "/"))

    if path == "/api/notes" and method == "GET":
        await _json_response(send, 200, {"notes": _list_notes()})
        return

    if path == "/api/notes" and method == "POST":
        payload = await _load_json_body(receive)
        note = _create_note(payload)
        await _json_response(send, 201, {"note": note})
        return

    note_id = _parse_note_id(path)
    if note_id is None:
        await _json_response(send, 404, _not_found_payload())
        return

    if method == "GET":
        note = _get_note(note_id)
        if note is None:
            await _json_response(send, 404, _not_found_payload())
        else:
            await _json_response(send, 200, {"note": note})
        return

    if method == "PUT":
        payload = await _load_json_body(receive)
        note = _update_note(note_id, payload)
        if note is None:
            await _json_response(send, 404, _not_found_payload())
        else:
            await _json_response(send, 200, {"note": note})
        return

    if method == "DELETE":
        deleted = _delete_note(note_id)
        if deleted:
            await _json_response(send, 200, {"deleted": True})
        else:
            await _json_response(send, 404, _not_found_payload())
        return

    await _json_response(send, 405, {"error": "Method not allowed"})


async def _load_json_body(receive: Any) -> dict[str, Any]:
    raw_body = await _read_body(receive)
    if not raw_body:
        return {}
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _static_file_for_path(path: str) -> tuple[Path, str]:
    if path in {"/", "/index.html"}:
        return STATIC_DIR / "index.html", "text/html; charset=utf-8"
    if path == "/styles.css":
        return STATIC_DIR / "styles.css", "text/css; charset=utf-8"
    if path == "/app.js":
        return STATIC_DIR / "app.js", "application/javascript; charset=utf-8"
    return STATIC_DIR / "index.html", "text/html; charset=utf-8"


async def _handle_static(scope: dict[str, Any], send: Any) -> None:
    path = unquote(scope.get("path", "/"))
    static_file, content_type = _static_file_for_path(path)
    if not static_file.exists():
        await _send_response(send, 404, b"Not found", "text/plain; charset=utf-8")
        return
    await _send_response(send, 200, static_file.read_bytes(), content_type)


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        return

    path = unquote(scope.get("path", "/"))
    if path.startswith("/api/"):
        await _handle_api(scope, receive, send)
    else:
        await _handle_static(scope, send)
