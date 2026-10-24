"""Fixed-route Atlas reads. Native JSON stays intact; no caller-supplied HTTP paths."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener
from xml.etree import ElementTree

from app.integrations.atlas_read import ATLAS_READ_SPECS
from app.services.github_provider import IntegrationProviderError
from app.services.local_document_reader import LocalDocumentReadError, extract_pdf_text

ATLAS_READ_OPERATIONS = frozenset(spec.id for spec in ATLAS_READ_SPECS)
_MAX_NATIVE_BYTES = 32 * 1024 * 1024
_MAX_PAGE_BYTES = 512 * 1024


def _error(kind: str = "invalid_input") -> IntegrationProviderError:
    return IntegrationProviderError(kind, "Atlas read could not be completed")


def _segment(value: str) -> str:
    if (not isinstance(value, str) or not value or value in {".", ".."} or len(value) > 128
            or any(char in value for char in "/\\?#%") or any(ord(char) < 32 for char in value)):
        raise _error()
    return quote(value, safe="")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _page(items: list[dict], requested: dict, scope: Any) -> dict:
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise _error("provider_unavailable")
    fingerprint = _digest([scope, items])
    offset = 0
    if cursor := requested.get("cursor"):
        try:
            decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(decoded, list) or len(decoded) != 2:
                raise ValueError
            observed, offset = decoded
            if type(offset) is not int or offset < 0 or offset > len(items):
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError):
            raise _error() from None
        if observed != fingerprint:
            raise _error("stale_revision")
    selected = []
    size = 0
    for item in items[offset:offset + requested.get("limit", 25)]:
        item_size = len(json.dumps(item, ensure_ascii=True).encode())
        if size + item_size > _MAX_PAGE_BYTES:
            if not selected:
                raise _error("response_too_large")
            break
        selected.append(item)
        size += item_size
    end = offset + len(selected)
    cursor = base64.urlsafe_b64encode(json.dumps([fingerprint, end]).encode()).decode() if end < len(items) else None
    return {"items": selected, "total": len(items), "next_cursor": cursor}


class AtlasReadProvider:
    def __init__(self, request: Callable[..., Any]) -> None:
        self.request = request

    def _get(self, path: str) -> Any:
        return self.request(path, None, timeout=15, max_bytes=_MAX_NATIVE_BYTES, method="GET")

    def _object(self, path: str) -> dict:
        value = self._get(path)
        if not isinstance(value, dict):
            raise _error("provider_unavailable")
        return value

    def execute(self, operation_id: str, requested: dict) -> dict:
        if operation_id not in ATLAS_READ_OPERATIONS:
            raise _error()
        # Keep direct provider callers bounded too; normal execution already validates at policy entry.
        from jsonschema import Draft202012Validator, ValidationError

        spec = next(spec for spec in ATLAS_READ_SPECS if spec.id == operation_id)
        try:
            Draft202012Validator(spec.input_schema).validate(requested)
        except ValidationError:
            raise _error() from None
        if operation_id == "atlas.describe":
            return {"description": self._describe(requested)}
        if operation_id == "atlas.records.search":
            params = {key: requested[key] for key in ("category", "trashed") if key in requested}
            if "query" in requested:
                params["q"] = requested["query"]
            items = self._get("/api/records" + ("?" + urlencode(params) if params else ""))
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise _error("provider_unavailable")
            if not requested.get("include_data", False):
                items = [{key: value for key, value in item.items()
                          if key in {"id", "category", "title", "revision", "trashed", "createdAt", "updatedAt"}}
                         for item in items]
            return _page(items, requested, [operation_id, params, requested.get("include_data", False)])
        if operation_id in {"atlas.records.get", "atlas.links.get"}:
            kind = "records" if operation_id == "atlas.records.get" else "links"
            return {"record" if kind == "records" else "link": self._object(f"/api/{kind}/{_segment(requested['id'])}")}
        if operation_id == "atlas.person.completeness.get":
            return {"completeness": self._object("/api/person/completeness")}
        if operation_id == "atlas.states.get":
            configuration = requested.get("view") == "configuration"
            if configuration and "at" in requested:
                raise _error()
            path = "/api/states/configuration" if configuration else "/api/states"
            if "at" in requested:
                path += "?" + urlencode({"at": requested["at"]})
            return {"states": self._object(path)}
        if operation_id == "atlas.personality.get":
            return {"personality": self._object("/api/personality")}
        if operation_id == "atlas.history.list":
            kind = requested["kind"]
            if kind == "states":
                if "id" in requested:
                    raise _error()
                return self._state_history(requested)
            else:
                if "id" not in requested:
                    raise _error()
                route = "records" if kind == "record" else "links"
                items = self._get(f"/api/{route}/{_segment(requested['id'])}/revisions")
            return _page(items, requested, [operation_id, kind, requested.get("id")])
        if operation_id == "atlas.graph.get":
            path = f"/api/goals/{_segment(requested['goal_id'])}/graph" if "goal_id" in requested else "/api/goal-graph"
            return {"graph": self._object(path)}
        if operation_id == "atlas.knowledge.read":
            view = requested.get("view", "nodes")
            if view == "node":
                if "node_id" not in requested or "cursor" in requested:
                    raise _error()
                data = self._object(f"/api/knowledge/nodes/{requested['node_id']}")
            else:
                if "node_id" in requested or (view == "tree" and "cursor" in requested):
                    raise _error()
                data = self._object("/api/knowledge/tree" if view == "tree" else "/api/knowledge/nodes")
                if view == "nodes":
                    page = _page(data.get("nodes"), requested, operation_id)
                    return {"data": page["items"], "next_cursor": page["next_cursor"], "total": page["total"]}
            return {"data": data, "next_cursor": None, "total": None}
        if operation_id == "atlas.attachments.list":
            record_id = _segment(requested["record_id"])
            record = self._object(f"/api/records/{record_id}")
            images = self._get(f"/api/records/{record_id}/images") if record.get("category") == "experience" else []
            resume = self._get(f"/api/records/{record_id}/resume") if record.get("category") == "person" else None
            if not isinstance(images, list) or (resume is not None and not isinstance(resume, dict)):
                raise _error("provider_unavailable")
            return {"images": images, "resume": resume}
        return self._attachment(requested)

    def _state_history(self, requested: dict) -> dict:
        limit = requested.get("limit", 25)
        first = self._object("/api/states/events?limit=1&offset=0")
        if (type(first.get("total")) is not int or first["total"] < 0
                or not isinstance(first.get("events"), list)):
            raise _error("provider_unavailable")
        fingerprint = _digest(["states", first["total"], first["events"]])
        offset = 0
        if cursor := requested.get("cursor"):
            try:
                decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(decoded, list) or len(decoded) != 2:
                    raise ValueError
                observed, offset = decoded
                if type(offset) is not int or not 0 <= offset <= first["total"]:
                    raise ValueError
            except (ValueError, TypeError, UnicodeDecodeError):
                raise _error() from None
            if observed != fingerprint:
                raise _error("stale_revision")
        page = self._object(f"/api/states/events?limit={limit}&offset={offset}")
        items = page.get("events")
        if (not isinstance(items, list) or any(not isinstance(item, dict) for item in items)
                or len(items) > limit or page.get("total") != first["total"]):
            raise _error("stale_revision")
        end = offset + len(items)
        if len(json.dumps(items, ensure_ascii=True).encode()) > _MAX_PAGE_BYTES:
            raise _error("response_too_large")
        if not items and end < first["total"]:
            raise _error("provider_unavailable")
        next_cursor = base64.urlsafe_b64encode(json.dumps([fingerprint, end]).encode()).decode() if end < first["total"] else None
        return {"items": items, "total": first["total"], "next_cursor": next_cursor}

    def _describe(self, requested: dict) -> dict:
        section = requested.get("section", "overview")
        if "path" in requested and section != "api_reference":
            raise _error()
        if "category" in requested and section != "custom_fields":
            raise _error()
        if section == "custom_fields":
            path = "/api/custom-fields"
            if "category" in requested:
                path += "?" + urlencode({"category": requested["category"]})
            return {"custom_fields": self._get(path)}
        if section == "model":
            return self._object("/api/model/catalog")
        reference = self._object("/api/settings/api-reference")
        if section == "api_reference":
            if "path" not in requested:
                return reference
            selected = {}
            for name, document in reference.items():
                if isinstance(document, dict) and requested["path"] in document.get("paths", {}):
                    selected[name] = {"paths": {requested["path"]: document["paths"][requested["path"]]},
                                      "components": document.get("components", {})}
            if not selected:
                raise _error("not_found")
            return selected
        return {
            "schemas": {name: document.get("components", {}) for name, document in reference.items()
                        if isinstance(document, dict)},
            "read_operations": [{"operation": spec.id, "description": spec.description} for spec in ATLAS_READ_SPECS],
            "guidance": "Discover schema; search compact records; fetch details only when relevant. "
                        "Native data and custom fields retain all fields and precision, including sensitive Person fields. "
                        "Completeness uses Atlas's UI definition. Follow cursors; stale_revision means restart the read. "
                        "Attachment text/bytes are explicit. Named record/link, lifecycle, custom-field, States and "
                        "attachment write operations have separate authorization. No setup, unlock, export, "
                        "Personality import or arbitrary HTTP operation is provided.",
        }

    def _attachment(self, requested: dict) -> dict:
        kind, attachment_id = requested["kind"], _segment(requested["id"])
        path = f"/api/images/{attachment_id}/content" if kind == "image" else f"/api/records/{attachment_id}/resume/content"
        if requested.get("format") == "text" and kind != "resume":
            raise _error()
        raw, media_type = _binary(path)
        digest = hashlib.sha256(raw).hexdigest()
        if "sha256" in requested and requested["sha256"] != digest:
            raise _error("stale_revision")
        content: bytes | str = raw
        encoding = requested.get("format", "base64")
        if encoding == "text":
            content = _resume_text(raw, media_type)
        offset, limit = requested.get("offset", 0), requested.get("limit", 65536)
        if offset > len(content):
            raise _error()
        end = min(len(content), offset + limit)
        chunk = content[offset:end]
        return {"content": base64.b64encode(chunk).decode() if isinstance(chunk, bytes) else chunk,
                "encoding": encoding, "media_type": media_type, "total": len(content),
                "next_offset": end if end < len(content) else None, "sha256": digest}


def _binary(path: str) -> tuple[bytes, str]:
    # Import lazily: the existing adapter imports this module for dispatch.
    from app.services.atlas_provider import ATLAS_BASE_URL, _NoRedirect

    try:
        response = build_opener(_NoRedirect()).open(Request(ATLAS_BASE_URL + path, method="GET"), timeout=15)
    except HTTPError as exc:
        exc.close()
        raise _error({423: "atlas_locked", 404: "not_found", 400: "invalid_input"}.get(exc.code, "provider_unavailable")) from None
    except TimeoutError:
        raise _error("provider_timeout") from None
    except (OSError, URLError):
        raise _error("provider_unavailable") from None
    try:
        if response.status != 200:
            raise _error("provider_unavailable")
        raw = response.read(20 * 1024 * 1024 + 1)
        media_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
    finally:
        response.close()
    if len(raw) > 20 * 1024 * 1024:
        raise _error("response_too_large")
    return raw, media_type


def _resume_text(raw: bytes, media_type: str) -> str:
    try:
        if media_type == "application/pdf":
            # A larger extraction cap allows paging without silently losing a document's tail.
            text = extract_pdf_text(raw, 2_000_001)
            if len(text) > 2_000_000:
                raise _error("response_too_large")
            return text
        if media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                part = archive.getinfo("word/document.xml")
                if part.file_size > 8 * 1024 * 1024:
                    raise _error("response_too_large")
                root = ElementTree.fromstring(archive.read(part))
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            return "\n".join("".join(node.itertext()) for node in root.iter(namespace + "p"))
    except (LocalDocumentReadError, zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise _error("provider_unavailable") from None
    raise _error("provider_unavailable")


def fake_read_output(operation_id: str, requested: dict) -> dict:
    """Deterministic provider outputs used by the existing test capability adapter."""
    if operation_id == "atlas.describe":
        return {"description": {}}
    if operation_id in {"atlas.records.search", "atlas.history.list"}:
        return {"items": [], "next_cursor": None, "total": 0}
    if operation_id == "atlas.knowledge.read":
        return {"data": [], "next_cursor": None, "total": 0}
    if operation_id == "atlas.attachments.list":
        return {"images": [], "resume": None}
    if operation_id == "atlas.attachments.read":
        return {"content": "", "encoding": requested.get("format", "base64"), "media_type": "application/pdf",
                "total": 0, "next_offset": None, "sha256": hashlib.sha256(b"").hexdigest()}
    key = {"atlas.records.get": "record", "atlas.links.get": "link", "atlas.person.completeness.get": "completeness",
           "atlas.states.get": "states", "atlas.personality.get": "personality", "atlas.graph.get": "graph"}[operation_id]
    return {key: {}}
