"""Notion todo manager web application."""

from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?$")
_TODO_FIELDS = {"title", "priority", "start_at", "due_at", "estimated_minutes", "atlas_goal_id", "notes"}
_ERROR_MESSAGES = {
    "connection_unavailable": "Notion is unavailable right now. Try again shortly.",
    "invalid_credential": "The Notion connection needs attention.",
    "authorization_missing_or_stale": "Notion authorization is missing or stale.",
    "invalid_input": "Notion rejected the todo details.",
    "not_found": "That todo could not be found.",
    "schema_mismatch": "Notion returned data this app could not understand.",
    "provider_forbidden": "The Notion connection is not allowed to perform this action.",
    "rate_limited": "Notion is rate limiting requests. Try again shortly.",
    "provider_timeout": "Notion took too long to respond. Try again shortly.",
    "response_too_large": "Notion returned more data than this app can display.",
    "provider_unavailable": "Notion is temporarily unavailable.",
    "operation_undeclared": "This Notion operation is not available.",
    "internal_failure": "The Notion operation could not be completed.",
}


class RequestError(ValueError):
    """A safe client-facing validation error."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _validate_string(value: Any, name: str, *, max_length: int = 2000) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        raise RequestError(f"{name} must be a non-empty string of at most {max_length} characters")
    return value


def _validate_optional_fields(payload: dict[str, Any]) -> None:
    unexpected = set(payload) - _TODO_FIELDS
    if unexpected:
        raise RequestError(f"Unsupported field: {sorted(unexpected)[0]}")
    if "priority" in payload:
        priority = payload["priority"]
        if priority is not None and priority not in ("low", "medium", "high"):
            raise RequestError("priority must be low, medium, high, or null")
    for name in ("start_at", "due_at"):
        if name in payload and payload[name] is not None:
            if not isinstance(payload[name], str) or not _DATE_PATTERN.fullmatch(payload[name]):
                raise RequestError(f"{name} must be an ISO date or date-time")
    if "estimated_minutes" in payload and payload["estimated_minutes"] is not None:
        value = payload["estimated_minutes"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RequestError("estimated_minutes must be a positive integer or null")
    for name in ("atlas_goal_id", "notes"):
        if name in payload and payload[name] is not None:
            _validate_string(payload[name], name)


def _validate_create(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RequestError("Request body must be a JSON object")
    if "title" not in payload:
        raise RequestError("title is required")
    _validate_string(payload["title"], "title")
    _validate_optional_fields(payload)
    return payload


def _validate_update(todo_id: str, payload: Any) -> dict[str, Any]:
    if not todo_id or len(todo_id) > 128:
        raise RequestError("id must be a non-empty string of at most 128 characters")
    if not isinstance(payload, dict):
        raise RequestError("Request body must be a JSON object")
    if "id" in payload and payload["id"] != todo_id:
        raise RequestError("The body id does not match the todo id")
    update = {"id": todo_id, **payload}
    if len(update) < 2:
        raise RequestError("At least one todo property is required")
    if "title" in update:
        _validate_string(update["title"], "title")
    _validate_optional_fields({key: value for key, value in update.items() if key != "id"})
    return update


def _validate_id(todo_id: str) -> str:
    if not todo_id or len(todo_id) > 128:
        raise RequestError("id must be a non-empty string of at most 128 characters")
    return todo_id


def _safe_error(exc: Exception) -> tuple[str, str]:
    error_type = getattr(exc, "error_type", "internal_failure")
    if error_type not in _ERROR_MESSAGES:
        error_type = "internal_failure"
    return error_type, _ERROR_MESSAGES[error_type]


async def _call_integration(operation: str, payload: dict[str, Any]) -> Any:
    """Call the platform-injected, instance-scoped integration helper."""
    from web_runtime_capabilities import call_integration

    result = call_integration(operation=operation, input=payload)
    if inspect.isawaitable(result):
        return await result
    return result


class TodoApp:
    """Minimal ASGI application for the persistent web-app runtime."""

    def __init__(self) -> None:
        self.package_dir = Path(__file__).resolve().parent
        self.cache_dir = Path(os.environ.get("PERSONAL_AGENT_SKILL_CACHE_DIR", "./cache"))

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope.get("type") != "http":
            return
        method = scope.get("method", "GET").upper()
        path, _query_string = self._target(scope)
        try:
            status, headers, body = await self._handle(method, path, scope, receive)
        except RequestError as exc:
            status, headers, body = self._json_response(400, {"error": "invalid_input", "message": str(exc)})
        except Exception as exc:
            error_type, message = _safe_error(exc)
            status, headers, body = self._json_response(502, {"error": error_type, "message": message})
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _handle(self, method: str, path: str, scope: dict[str, Any], receive: Any) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        if method == "GET" and path == "/__personal_agent__/health":
            return self._json_response(200, {"status": "ok"})
        if method == "GET" and path == "/":
            return self._asset_response("index.html", "text/html; charset=utf-8")
        asset_types = {
            "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/static/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if method == "GET" and path in asset_types:
            filename, content_type = asset_types[path]
            return self._asset_response(filename, content_type)

        if path == "/api/todos" and method == "GET":
            _path, query_string = self._target(scope)
            query = parse_qs(query_string)
            page_size_text = query.get("page_size", ["25"])[0]
            try:
                page_size = int(page_size_text)
            except ValueError as exc:
                raise RequestError("page_size must be an integer") from exc
            if not 1 <= page_size <= 100:
                raise RequestError("page_size must be between 1 and 100")
            payload: dict[str, Any] = {"page_size": page_size}
            if "start_cursor" in query:
                cursor = query["start_cursor"][0]
                if not 1 <= len(cursor) <= 2048:
                    raise RequestError("start_cursor is invalid")
                payload["start_cursor"] = cursor
            result = await _call_integration("notion.todo.list", payload)
            return self._json_response(200, result)

        prefix = "/api/todos/"
        if path.startswith(prefix) and len(path) > len(prefix):
            todo_id = _validate_id(unquote(path[len(prefix) :]))
            if method == "DELETE":
                result = await _call_integration("notion.todo.delete", {"id": todo_id})
                return self._json_response(200, result)
            if method == "PATCH":
                body = await self._read_json(receive)
                result = await _call_integration("notion.todo.update", _validate_update(todo_id, body))
                return self._json_response(200, result)

        if path == "/api/todos" and method == "POST":
            body = await self._read_json(receive)
            result = await _call_integration("notion.todo.create", _validate_create(body))
            return self._json_response(200, result)
        return self._json_response(404, {"error": "not_found", "message": "Route not found"})

    async def _read_json(self, receive: Any) -> Any:
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 1_048_576:
                raise RequestError("Request body is too large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("Request body must be valid JSON") from exc

    def _asset_response(self, filename: str, content_type: str) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        body = (self.package_dir / "static" / filename).read_bytes()
        return 200, self._headers(content_type, len(body)), body

    @staticmethod
    def _target(scope: dict[str, Any]) -> tuple[str, str]:
        raw_path = scope.get("raw_path", scope.get("path", "/"))
        if isinstance(raw_path, bytes):
            raw_path = raw_path.decode("utf-8", errors="replace")
        parsed = urlsplit(raw_path)
        return parsed.path, parsed.query

    @staticmethod
    def _headers(content_type: str, length: int) -> list[tuple[bytes, bytes]]:
        return [(b"content-type", content_type.encode("ascii")), (b"content-length", str(length).encode("ascii")), (b"cache-control", b"no-store")]

    def _json_response(self, status: int, value: Any) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        body = _json_bytes(value)
        return status, self._headers("application/json; charset=utf-8", len(body)), body


app = TodoApp()
