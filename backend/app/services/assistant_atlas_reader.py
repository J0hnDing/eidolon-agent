"""Read all current Atlas content for initialization without persisting source bodies."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.services.integration_service import IntegrationError, build_default_integration_service

_JSON_CHARS = 24_000
_IMAGE_BYTES = 20 * 1024 * 1024
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _task(operation: str, **arguments) -> dict:
    return {"operation": operation, "input": arguments}


def initial_tasks(at: str) -> list[dict]:
    """Build current Atlas reads; ``at`` remains for existing callers and queue metadata."""
    return [
        *[_task("atlas.describe", section=section) for section in ("overview", "model", "custom_fields")],
        _task("atlas.records.search", include_data=False, trashed="false", limit=100),
        _task("atlas.graph.get"),
        _task("atlas.states.get", view="current"),
        _task("atlas.states.get", view="configuration"),
        _task("atlas.history.list", kind="states", limit=100),
        _task("atlas.personality.get"),
        _task("atlas.person.completeness.get"),
        _task("atlas.knowledge.read", view="nodes", limit=100),
    ]


def _invalid(message: str = "Atlas initialization response is incomplete") -> IntegrationError:
    return IntegrationError("provider_unavailable", message)


def _items(value: object) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _invalid()
    return value


def _next_tasks(task: dict, output: dict) -> list[dict]:
    operation, arguments = task["operation"], task["input"]
    tasks = []
    if operation in {"atlas.records.search", "atlas.history.list", "atlas.knowledge.read"}:
        is_nodes = operation == "atlas.knowledge.read" and arguments.get("view", "nodes") == "nodes"
        is_page = operation != "atlas.knowledge.read" or is_nodes
        if is_page:
            items = _items(output.get("data" if is_nodes else "items"))
            if operation == "atlas.records.search":
                for item in items:
                    if not isinstance(item.get("id"), str) or not item["id"]:
                        raise _invalid()
                    tasks.append(_task("atlas.records.get", id=item["id"]))
            elif is_nodes:
                for item in items:
                    if type(item.get("id")) is not int or item["id"] < 1:
                        raise _invalid()
                    tasks.append(_task("atlas.knowledge.read", view="node", node_id=item["id"]))
            cursor = output.get("next_cursor")
            if cursor is not None:
                if not isinstance(cursor, str) or not cursor or cursor == arguments.get("cursor"):
                    raise _invalid("Atlas initialization pagination did not advance")
                tasks.append({"operation": operation, "input": {**arguments, "cursor": cursor}})
    elif operation == "atlas.records.get":
        record = output.get("record")
        if not isinstance(record, dict):
            raise _invalid()
        if record.get("category") in {"person", "experience"}:
            tasks.append(_task("atlas.attachments.list", record_id=arguments["id"]))
        for link in _items(record.get("links", [])) + _items(record.get("backlinks", [])):
            if not isinstance(link.get("id"), str) or not link["id"]:
                raise _invalid()
            tasks.append(_task("atlas.links.get", id=link["id"]))
    elif operation == "atlas.attachments.list":
        for image in _items(output.get("images")):
            if not isinstance(image.get("id"), str) or not image["id"]:
                raise _invalid()
            tasks.append(_task("atlas.attachments.read", kind="image", id=image["id"], format="base64"))
        if output.get("resume") is not None:
            if not isinstance(output["resume"], dict):
                raise _invalid()
            tasks.append(_task("atlas.attachments.read", kind="resume", id=arguments["record_id"],
                               format="text", limit=_JSON_CHARS))
    elif operation == "atlas.attachments.read" and arguments.get("kind") == "resume":
        content, total, offset = output.get("content"), output.get("total"), arguments.get("offset", 0)
        if (output.get("encoding") != "text" or not isinstance(content, str)
                or type(total) is not int or total <= 0):
            raise _invalid("Atlas resume has no readable text; initialization cannot claim it was read")
        end = offset + len(content)
        next_offset = output.get("next_offset")
        if end > total or (next_offset is None and end != total) or (
                next_offset is not None and (type(next_offset) is not int or next_offset != end or end <= offset)):
            raise _invalid("Atlas resume pagination is incomplete")
        if next_offset is not None:
            tasks.append({"operation": operation, "input": {
                **arguments, "offset": next_offset, "sha256": output["sha256"],
            }})
    return tasks


def _image(read, arguments: dict) -> dict:
    """Assemble native byte pages into one model-visible image, never base64 text."""
    offset, raw, expected = 0, bytearray(), None
    media_type, digest, total = None, None, None
    while True:
        requested = {**arguments, "format": "base64", "offset": offset, "limit": 131072}
        if expected is not None:
            requested["sha256"] = digest
        output = read("atlas.attachments.read", requested)
        signature = (output.get("sha256"), output.get("media_type"), output.get("total"))
        if expected is None:
            digest, media_type, total = signature
            if (not isinstance(digest, str) or len(digest) != 64 or media_type not in _IMAGE_TYPES
                    or type(total) is not int or not 0 < total <= _IMAGE_BYTES):
                raise _invalid("Atlas image metadata is unsupported or incomplete")
            expected = signature
        elif signature != expected:
            raise IntegrationError("stale_revision", "Atlas image changed during initialization")
        try:
            if output.get("encoding") != "base64":
                raise ValueError
            chunk = base64.b64decode(output["content"], validate=True)
        except (binascii.Error, ValueError, TypeError, KeyError):
            raise _invalid("Atlas image bytes are invalid") from None
        raw.extend(chunk)
        end = len(raw)
        next_offset = output.get("next_offset")
        if end > total or not chunk or (next_offset is not None and (
                type(next_offset) is not int or next_offset != end)):
            raise _invalid("Atlas image pagination is incomplete")
        if next_offset is None:
            if end != total or hashlib.sha256(raw).hexdigest() != digest:
                raise _invalid("Atlas image content failed verification")
            matches_type = {
                "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
                "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
                "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
            }
            if not matches_type[media_type]:
                raise _invalid("Atlas image bytes do not match their media type")
            return {"data": {"attachment": {"kind": "image", "id": arguments["id"],
                                             "sha256": digest, "media_type": media_type, "size": total}},
                    "next_tasks": [], "continuation": None,
                    "_initialization_image": {"data": base64.b64encode(raw).decode(), "mime_type": media_type}}
        offset = end


def read_task(db: Session, task: dict, *, session_id: int, turn_id: int, at: str) -> dict:
    """Fetch a typed task; return a complete object or an explicitly marked JSON slice.

    Continuations contain only the source reference, offset and digest. Every slice
    refetches the authoritative object; a changed object restarts from its beginning.
    No source text or image bytes are written to the initialization queue.
    """
    service = build_default_integration_service(db)
    context = InvocationContext(principal_kind="agent", origin="backend", agent_id="assistant",
                                agent_session_id=session_id, agent_turn_id=turn_id,
                                initiating_action="assistant.atlas_initialization")

    def read(operation: str, arguments: dict) -> dict:
        output = service.execute_context(context, operation, arguments).output
        if not isinstance(output, dict):
            raise _invalid()
        return output

    operation, arguments = task["operation"], dict(task["input"])
    if operation == "atlas.states.get" and arguments.get("view") == "current":
        # Initialization resumes the live current view. Older durable queues stored
        # the pass's original timestamp here, which can become invalid or obsolete.
        # Keep the public integration's explicit historical-at behavior unchanged.
        arguments.pop("at", None)
    if operation == "atlas.attachments.read" and arguments.get("kind") == "image":
        return _image(read, arguments)
    output = read(operation, arguments)
    next_tasks = _next_tasks({"operation": operation, "input": arguments}, output)
    text = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    digest = hashlib.sha256(text.encode()).hexdigest()
    offset = task.get("offset", 0) if task.get("sha256") == digest else 0
    if type(offset) is not int or offset < 0 or offset >= len(text):
        offset = 0
    if len(text) <= _JSON_CHARS:
        return {"data": output, "next_tasks": next_tasks, "continuation": None}
    end = min(len(text), offset + _JSON_CHARS)
    continuation = {"operation": operation, "input": arguments, "offset": end, "sha256": digest} \
        if end < len(text) else None
    return {"data": {"format": "json_slice", "content": text[offset:end], "offset": offset,
                     "total": len(text), "sha256": digest},
            "next_tasks": next_tasks if continuation is None else [], "continuation": continuation}
