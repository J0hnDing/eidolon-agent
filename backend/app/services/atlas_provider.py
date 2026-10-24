from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jsonschema import Draft202012Validator

from app.integrations.atlas_attachments import ATLAS_ATTACHMENT_OPERATIONS
from app.integrations.atlas_lifecycle import ATLAS_LIFECYCLE_OPERATIONS
from app.integrations.atlas_state_write import ATLAS_STATE_WRITE_OPERATIONS
from app.integrations.atlas_write import ATLAS_WRITE_OPERATIONS
from app.integrations.types import IntegrationOperationSpec
from app.services.atlas_attachment_provider import AtlasAttachmentProvider
from app.services.atlas_mutation_provider import AtlasMutationProvider
from app.services.atlas_read_provider import ATLAS_READ_OPERATIONS, AtlasReadProvider, _segment, fake_read_output
from app.services.atlas_state_write_provider import AtlasStateWriteProvider
from app.services.github_provider import IntegrationProviderError

ATLAS_BASE_URL = "http://127.0.0.1:4817"


class AtlasProviderAdapter(Protocol):
    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
    ) -> dict[str, Any]: ...

    def establish(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AtlasTransportOperation:
    """Provider-private request bounds, separate from the semantic operation contract."""

    operation_id: str
    timeout_seconds: float
    max_provider_response_bytes: int


_TRANSPORT_OPERATIONS = {
    value.operation_id: value
    for value in (
        AtlasTransportOperation("atlas.person.get", 10, 1_000_000),
        AtlasTransportOperation("atlas.interest.get", 10, 2_000_000),
        AtlasTransportOperation("atlas.interest.list", 10, 2_000_000),
        AtlasTransportOperation("atlas.experience.list", 10, 2_000_000),
        AtlasTransportOperation("atlas.goal.list", 10, 3_000_000),
        AtlasTransportOperation("atlas.goal.create", 10, 1_000_000),
        AtlasTransportOperation("atlas.goal.update", 10, 1_000_000),
        AtlasTransportOperation("atlas.project.list", 10, 2_000_000),
        AtlasTransportOperation("atlas.relationship.list", 10, 3_000_000),
        AtlasTransportOperation("atlas.knowledge.frontier.list", 10, 2_000_000),
        AtlasTransportOperation("atlas.knowledge.search", 10, 1_000_000),
        AtlasTransportOperation("atlas.knowledge.node.get", 10, 1_000_000),
        AtlasTransportOperation("atlas.knowledge.node.know", 180, 2_000_000),
    )
}


def _transport_operation(operation: IntegrationOperationSpec) -> AtlasTransportOperation:
    try:
        return _TRANSPORT_OPERATIONS[operation.id]
    except KeyError:
        raise IntegrationProviderError("internal_failure", "Atlas operation is unsupported") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


class UrllibAtlasProviderAdapter:
    """Fixed-route adapter for Atlas's trusted unlocked loopback API."""

    _record_routes = {
        "atlas.person.get": "/api/records?category=person",
        "atlas.interest.get": "/api/records?category=interest",
        "atlas.interest.list": "/api/records?category=interest",
        "atlas.experience.list": "/api/records?category=experience",
        "atlas.goal.list": "/api/records?category=goal",
        "atlas.project.list": "/api/records?category=project",
        "atlas.relationship.list": "/api/records?category=relationship",
    }

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
    ) -> dict[str, Any]:
        if operation.id in ATLAS_LIFECYCLE_OPERATIONS:
            return AtlasMutationProvider(self._request).execute(operation.id, input_json)
        if operation.id in ATLAS_STATE_WRITE_OPERATIONS:
            return AtlasStateWriteProvider(self._request).execute(operation.id, input_json)
        if operation.id in ATLAS_ATTACHMENT_OPERATIONS:
            return AtlasAttachmentProvider(self._request).execute(operation.id, input_json)
        if operation.id in ATLAS_WRITE_OPERATIONS:
            if not Draft202012Validator(operation.input_schema).is_valid(input_json):
                raise IntegrationProviderError("invalid_input", "Atlas write input does not match its schema")
            kind, action = operation.id.split(".")[1:]
            payload = {key: value for key, value in input_json.items() if key not in {"id", "expected_revision"}}
            path = f"/api/{kind}"
            if action == "update":
                path += "/" + _segment(input_json["id"])
                payload["revision"] = input_json["expected_revision"]
            result = self._request(path, payload, timeout=10, max_bytes=2_000_000,
                                   method="POST" if action == "create" else "PATCH")
            if not isinstance(result, dict) or not isinstance(result.get("id"), str):
                raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid write result")
            return {"record" if kind == "records" else "link": result}
        if operation.id in ATLAS_READ_OPERATIONS:
            return AtlasReadProvider(self._request).execute(operation.id, input_json)
        operation = _transport_operation(operation)
        if operation.operation_id == "atlas.goal.create":
            return self._create_goal(input_json, operation)
        if operation.operation_id == "atlas.goal.update":
            return self._update_goal(input_json, operation)
        route = self._record_routes.get(operation.operation_id)
        if route is not None:
            payload = self._request(
                route,
                None,
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
                method="GET",
            )
            if operation.operation_id == "atlas.person.get":
                records = self._record_list(payload, "person")
                resume = self._request(
                    f"/api/records/{quote(records[0]['id'], safe='')}/resume",
                    None,
                    timeout=operation.timeout_seconds,
                    max_bytes=64_000,
                    method="GET",
                ) if records else None
                completeness = self._request(
                    "/api/person/completeness", None,
                    timeout=operation.timeout_seconds,
                    max_bytes=operation.max_provider_response_bytes, method="GET",
                )
                if not isinstance(completeness, dict):
                    raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Person completeness")
                payload = {"records": records, "resume": resume, "completeness": completeness}
            if operation.operation_id == "atlas.goal.list":
                records = self._record_list(payload, "goal")
                graph = self._request(
                    "/api/goal-graph", None, timeout=operation.timeout_seconds,
                    max_bytes=operation.max_provider_response_bytes, method="GET",
                )
                if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
                    raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Goal graph")
                payload = {"records": records, "graph": graph}
        elif operation.operation_id.startswith("atlas.knowledge."):
            payload = self._request(
                "/api/knowledge/nodes",
                None,
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
                method="GET",
            )
        else:
            raise IntegrationProviderError("internal_failure", "Atlas operation is unsupported")
        return self._normalize(operation.operation_id, payload, input_json)

    def establish(self, payload: dict[str, Any]) -> dict[str, Any]:
        nodes = self._knowledge_nodes(
            self._request(
                "/api/knowledge/nodes", None, timeout=10, max_bytes=2_000_000, method="GET"
            )
        )
        by_id = self._knowledge_by_id(nodes)
        target = by_id.get(int(payload["node_id"]))
        if target is None:
            raise IntegrationProviderError("not_found", "Atlas Knowledge node was not found")
        if int(target["revision"]) != int(payload["expected_revision"]):
            raise IntegrationProviderError("stale_revision", "Atlas Knowledge node changed before update")
        if target.get("status") == "known":
            raise IntegrationProviderError("node_already_known", "Atlas Knowledge node is already known")
        existing_by_name = {
            str(node["name"]).casefold(): str(node["name"])
            for node in nodes
            if node.get("parentId") == target["id"]
        }
        updated = self._request(
            f"/api/knowledge/nodes/{target['id']}",
            {"status": "known", "understanding": payload["explanation"], "terms": payload["terms"]},
            timeout=20,
            max_bytes=2_000_000,
            method="PATCH",
        )
        if not isinstance(updated, dict) or not isinstance(updated.get("node"), dict):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned an invalid Knowledge result")
        created_children: list[str] = []
        existing_children: list[str] = []
        for child_name in payload["children"]:
            key = str(child_name).casefold()
            if key in existing_by_name:
                existing_children.append(existing_by_name[key])
                continue
            self._request(
                "/api/knowledge/nodes",
                {
                    "name": child_name,
                    "branch": target["branch"],
                    "parentId": target["id"],
                    "status": "unassessed",
                },
                timeout=20,
                max_bytes=2_000_000,
                method="POST",
            )
            existing_by_name[key] = child_name
            created_children.append(child_name)
        refreshed = self._knowledge_nodes(
            self._request(
                "/api/knowledge/nodes", None, timeout=10, max_bytes=2_000_000, method="GET"
            )
        )
        final_node = self._bounded_knowledge_node(int(target["id"]), refreshed)
        return {
            "node": final_node,
            "created_children": created_children,
            "existing_children": existing_children,
        }

    _goal_fields = {
        "description": "description", "success_conditions": "successConditions",
        "horizon": "horizon", "target_date": "targetDate", "pursuit_status": "pursuitStatus",
        "commitment": "commitment", "blueprint": "blueprint", "provenance": "provenance",
    }

    @classmethod
    def _goal_input(cls, requested: dict[str, Any], *, update: bool) -> None:
        allowed = {"title", "custom_field_values", *cls._goal_fields}
        if update:
            allowed |= {"id", "expected_revision"}
        if set(requested) - allowed:
            raise IntegrationProviderError("invalid_input", "Unsupported Goal fields; use the current Goal blueprint contract")

    def _create_goal(
        self, requested: dict[str, Any], operation: AtlasTransportOperation
    ) -> dict[str, Any]:
        self._goal_input(requested, update=False)
        data = {
            "horizon": "short", "description": "", "successConditions": "",
            "pursuitStatus": "considered", "commitment": 0, "blueprint": {"nodes": [], "edges": []},
        }
        self._patch_goal_data(data, requested)
        record = self._request(
            "/api/records",
            {"category": "goal", "title": requested["title"], "data": data,
             "customFieldValues": requested.get("custom_field_values", {})},
            timeout=operation.timeout_seconds, max_bytes=operation.max_provider_response_bytes, method="POST",
        )
        return {"goal": self._goal_view(record), "created": True}

    def _update_goal(
        self, requested: dict[str, Any], operation: AtlasTransportOperation
    ) -> dict[str, Any]:
        self._goal_input(requested, update=True)
        if not set(requested) - {"id", "expected_revision"}:
            raise IntegrationProviderError("invalid_input", "A Goal edit must change at least one field")
        goal_id = requested["id"]
        record = self._goal_record(goal_id, operation)
        if record["trashed"]:
            raise IntegrationProviderError("invalid_input", "A removed Goal cannot be updated")
        if record["revision"] != requested["expected_revision"]:
            raise IntegrationProviderError("stale_revision", "Atlas Goal changed before update")
        patch: dict[str, Any] = {"revision": requested["expected_revision"]}
        if "title" in requested:
            patch["title"] = requested["title"]
        if set(requested) & self._goal_fields.keys():
            data = dict(record["data"])
            self._patch_goal_data(data, requested)
            patch["data"] = data
        if "custom_field_values" in requested:
            patch["customFieldValues"] = requested["custom_field_values"]
        updated = self._request(
            f"/api/records/{quote(goal_id, safe='')}", patch,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes, method="PATCH",
        )
        if not isinstance(updated, dict) or updated.get("id") != goal_id:
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid updated Goal")
        return {"goal": self._goal_view(updated)}

    @classmethod
    def _patch_goal_data(cls, data: dict[str, Any], requested: dict[str, Any]) -> None:
        for key, atlas_key in cls._goal_fields.items():
            if key not in requested:
                continue
            if key == "target_date":
                if requested[key] is None:
                    data.pop(atlas_key, None)
                    continue
                data[atlas_key] = cls._goal_target_date(requested[key])
            else:
                data[atlas_key] = requested[key]

    def _goal_record(self, goal_id: str, operation: AtlasTransportOperation) -> dict[str, Any]:
        record = self._request(
            f"/api/records/{quote(goal_id, safe='')}", None,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes, method="GET",
        )
        if (not isinstance(record, dict) or record.get("id") != goal_id
                or self._goal_record_shape_invalid(record) or not isinstance(record.get("trashed"), bool)):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Goal data")
        return record

    @classmethod
    def _goal_view(cls, record: Any) -> dict[str, Any]:
        if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
                or cls._goal_record_shape_invalid(record)):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Goal data")
        progress = record.get("progress")
        if progress is not None and (not isinstance(progress, (int, float)) or isinstance(progress, bool)
                or not math.isfinite(progress) or not 0 <= progress <= 100):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Goal progress")
        data = record["data"]
        return {
            "id": record["id"], "title": record["title"], "revision": record["revision"],
            "description": data.get("description", ""), "success_conditions": data.get("successConditions", ""),
            "horizon": data.get("horizon"), "target_date": data.get("targetDate"), "progress": progress,
            "pursuit_status": data.get("pursuitStatus", "considered"), "commitment": data.get("commitment", 0),
            "blueprint": data.get("blueprint", {"nodes": [], "edges": []}),
            "provenance": data.get("provenance", {"attribution": "unknown", "evidence": []}),
            "custom_field_values": record.get("customFieldValues", {}), "data": dict(data),
            "grounding_status": record.get("groundingStatus"), "connection_status": record.get("connectionStatus"),
        }

    @staticmethod
    def _goal_target_date(value: Any) -> str:
        if not isinstance(value, str):
            raise IntegrationProviderError("invalid_input", "Goal target date is invalid")
        try:
            if not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?", value):
                raise ValueError
            if len(value) == 4:
                date(int(value), 1, 1)
            elif len(value) == 7:
                date(int(value[:4]), int(value[5:]), 1)
            elif len(value) == 10:
                date.fromisoformat(value)
            else:
                raise ValueError
        except ValueError:
            raise IntegrationProviderError("invalid_input", "Goal target date is invalid") from None
        return value

    @staticmethod
    def _goal_record_shape_invalid(record: dict[str, Any]) -> bool:
        return (
            record.get("category") != "goal"
            or not isinstance(record.get("title"), str)
            or not record["title"]
            or not isinstance(record.get("revision"), int)
            or isinstance(record.get("revision"), bool)
            or record["revision"] < 1
            or not isinstance(record.get("data"), dict)
            or (record.get("parentId") is not None and not isinstance(record["parentId"], str))
        )

    def _normalize(self, operation_id: str, payload: Any, requested: dict[str, Any]) -> dict[str, Any]:
        if operation_id == "atlas.relationship.list":
            items = self._relationship_records(payload)
            keywords = self._keywords(requested)
            if keywords:
                items = [item for item in items if self._matches(item, ("name", "relationship_type", "notes"), keywords)]
            for requested_name, candidate_names in (
                ("kind", ("category",)),
                ("status", ("status",)),
                ("importance", ("importance",)),
            ):
                value = requested.get(requested_name)
                if value:
                    items = [item for item in items if any(item.get(name) == value for name in candidate_names)]
            return {"relationships": items[: self._limit(requested)]}
        if operation_id.startswith("atlas.knowledge."):
            nodes = self._knowledge_nodes(payload)
            if operation_id == "atlas.knowledge.frontier.list":
                return self._knowledge_frontier(nodes, requested)
            if operation_id == "atlas.knowledge.search":
                return self._knowledge_search(nodes, requested)
            if operation_id == "atlas.knowledge.node.get":
                return {"node": self._bounded_knowledge_node(int(requested["node_id"]), nodes)}
        if operation_id == "atlas.person.get":
            records = payload["records"]
            if not records:
                return {"personal_info": None, "completeness": payload["completeness"]}
            record = records[0]
            data = record["data"]
            resume = payload["resume"]
            if resume is not None and (
                not isinstance(resume, dict)
                or not isinstance(resume.get("filename"), str)
                or not isinstance(resume.get("mimeType"), str)
                or not isinstance(resume.get("byteLength"), int)
            ):
                raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid resume metadata")
            return {
                "completeness": payload["completeness"],
                "personal_info": {
                    "id": record["id"], "revision": record.get("revision"),
                    "data": {key: value for key, value in data.items() if key not in {
                        "passportNumber", "nationalIdNumber", "driversLicenseNumber", "taxIdNumber", "nationalId",
                    }},
                    "custom_field_values": record.get("customFieldValues", {}),
                    "name": record["title"],
                    "preferred_name": data.get("preferredName"),
                    "gender": data.get("gender"),
                    "birth_date": data.get("birthDate"),
                    "birth_place": data.get("birthPlace"),
                    "nationalities": data.get("nationalities") if isinstance(data.get("nationalities"), list) else [],
                    "languages": data.get("languages") if isinstance(data.get("languages"), list) else [],
                    "marital_status": data.get("maritalStatus"),
                    "emails": data.get("emails") if isinstance(data.get("emails"), list) else [],
                    "phone_numbers": data.get("phoneNumbers") if isinstance(data.get("phoneNumbers"), list) else [],
                    "address": data.get("address"),
                    "postal_code": data.get("postalCode"),
                    "occupation": data.get("occupation"),
                    "instagram": data.get("instagram"),
                    "discord": data.get("discord"),
                    "linkedin_link": data.get("linkedinLink"),
                    "wechat_number": data.get("wechatNumber"),
                    "facebook": data.get("facebook"),
                    "personal_website": data.get("personalWebsite"),
                    "resume": None if resume is None else {
                        "filename": resume["filename"],
                        "mime_type": resume["mimeType"],
                        "byte_length": resume["byteLength"],
                        "created_at": resume.get("createdAt"),
                        "updated_at": resume.get("updatedAt"),
                    },
                    "summary": data.get("summary"),
                    "notes": data.get("notes"),
                }
            }
        if operation_id in {"atlas.interest.get", "atlas.interest.list"}:
            return self._interest_records(payload)
        if operation_id == "atlas.experience.list":
            records = self._record_list(payload, "experience")
            records.sort(
                key=lambda record: (
                    str(record["data"].get("startDate") or ""),
                    str(record.get("createdAt") or ""),
                ),
                reverse=True,
            )
            items = [
                {
                    **self._record_content(record),
                    "title": record["title"],
                    "time": {
                        "start_date": record["data"].get("startDate"),
                        "end_date": record["data"].get("endDate") or None,
                        "ongoing": record["data"].get("ongoing"),
                    },
                    "description": record["data"].get("narrative"),
                }
                for record in records
            ]
            keywords = self._keywords(requested)
            ongoing = requested.get("ongoing")
            if keywords:
                items = [item for item in items if self._matches(item, ("title", "description"), keywords)]
            if isinstance(ongoing, bool):
                items = [item for item in items if (item.get("time") or {}).get("ongoing") is ongoing]
            return {"experiences": items[: self._limit(requested)]}
        if operation_id == "atlas.goal.list":
            if set(requested) - {"horizon", "pursuit_status", "limit"}:
                raise IntegrationProviderError("invalid_input", "Unsupported Goal filters")
            goals = [self._goal_view(record) for record in self._record_list(payload["records"], "goal")]
            for name in ("horizon", "pursuit_status"):
                if name in requested:
                    goals = [goal for goal in goals if goal.get(name) == requested[name]]
            return {"goals": goals[: self._limit(requested)], "graph": payload["graph"]}
        if operation_id == "atlas.project.list":
            items = [
                {
                    "title": record["title"],
                    **self._record_content(record),
                    "description": record["data"].get("context"),
                    "status": record["data"].get("status"),
                    "github_link": record["data"].get("githubLink") or None,
                }
                for record in self._record_list(payload, "project")
            ]
            items.sort(key=lambda item: str(item["title"]).casefold())
            items = self._filter_keyword_status(items, requested, ("title", "description"))
            has_link = requested.get("has_github_link")
            if isinstance(has_link, bool):
                items = [item for item in items if bool(item.get("github_link")) is has_link]
            return {"projects": items[: self._limit(requested)]}
        raise IntegrationProviderError("internal_failure", "Atlas operation is unsupported")

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | bytes | None,
        *,
        timeout: float,
        max_bytes: int,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            raw_input = payload if isinstance(payload, bytes) else (
                None if payload is None else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        except (ValueError, TypeError, UnicodeEncodeError):
            raise IntegrationProviderError("invalid_input", "Atlas input must be finite UTF-8 JSON") from None
        request_headers = {"Accept": "application/json"}
        if raw_input is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = Request(
            f"{ATLAS_BASE_URL}{path}",
            data=raw_input,
            method=method,
            headers=request_headers,
        )
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=timeout)
        except HTTPError as exc:
            raw_error = exc.read(65_537)
            code = self._error_code(raw_error)
            if 300 <= exc.code < 400:
                error_type = "provider_unavailable"
            elif exc.code in {401, 403}:
                error_type = "provider_forbidden"
            elif exc.code == 423:
                error_type = "atlas_locked"
            elif exc.code == 404:
                error_type = "not_found"
            elif exc.code == 409:
                error_type = {"REVISION_CONFLICT": "stale_revision", "EVENT_ID_CONFLICT": "event_id_conflict",
                              "node_already_known": "node_already_known"}.get(code, "invalid_input")
            elif exc.code in {400, 415}:
                error_type = "invalid_input"
            elif exc.code == 413:
                error_type = "request_too_large"
            else:
                error_type = "provider_unavailable"
            exc.close()
            raise IntegrationProviderError(error_type, "Atlas could not complete the operation") from None
        except TimeoutError:
            raise IntegrationProviderError("provider_timeout", "Atlas did not respond before the timeout") from None
        except (OSError, URLError):
            raise IntegrationProviderError("provider_unavailable", "Atlas is unavailable") from None
        try:
            if 300 <= int(response.status) < 400:
                raise IntegrationProviderError("provider_unavailable", "Atlas redirects are not accepted")
            raw = response.read(max_bytes + 1)
        finally:
            response.close()
        if len(raw) > max_bytes:
            raise IntegrationProviderError("response_too_large", "Atlas response exceeded the size limit")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationProviderError("provider_unavailable", "Atlas returned an invalid response") from exc

    @staticmethod
    def _error_code(raw: bytes) -> str | None:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        error = payload.get("error") if isinstance(payload, dict) else None
        return str(error.get("code")) if isinstance(error, dict) and error.get("code") else None

    @staticmethod
    def _items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned an invalid response")
        return [dict(item) for item in value]

    @staticmethod
    def _record_list(payload: Any, category: str) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise IntegrationProviderError("provider_unavailable", f"Atlas returned invalid {category.title()} data")
        records: list[dict[str, Any]] = []
        for item in payload:
            if (
                item.get("category") != category
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("title"), str)
                or not isinstance(item.get("data"), dict)
            ):
                raise IntegrationProviderError("provider_unavailable", f"Atlas returned invalid {category.title()} data")
            records.append({**item, "data": dict(item["data"])})
        return records

    @staticmethod
    def _keywords(value: dict[str, Any]) -> str:
        return str(value.get("keywords") or "").strip().casefold()

    @staticmethod
    def _matches(item: dict[str, Any], fields: tuple[str, ...], keywords: str) -> bool:
        return any(keywords in str(item.get(field) or "").casefold() for field in fields)

    @classmethod
    def _filter_keyword_status(
        cls, items: list[dict[str, Any]], requested: dict[str, Any], fields: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        keywords = cls._keywords(requested)
        if keywords:
            items = [item for item in items if cls._matches(item, fields, keywords)]
        status = requested.get("status")
        return [item for item in items if not status or item.get("status") == status]

    @staticmethod
    def _limit(value: dict[str, Any]) -> int:
        return min(int(value.get("limit", 25)), 100)

    @staticmethod
    def _record_content(record: dict[str, Any]) -> dict[str, Any]:
        return {"id": record["id"], "revision": record.get("revision"), "data": dict(record["data"]),
                "custom_field_values": record.get("customFieldValues", {})}

    @classmethod
    def _relationship_records(cls, payload: Any) -> list[dict[str, Any]]:
        relationships = []
        for record in cls._record_list(payload, "relationship"):
            data = record["data"]
            relationships.append(
                {
                    **cls._record_content(record),
                    "name": str(record.get("title") or ""),
                    "category": data.get("kind"),
                    "relationship_type": data.get("relationshipType") or None,
                    "status": data.get("status") or None,
                    "importance": data.get("importance") or None,
                    "start_date": data.get("startDate") or None,
                    "end_date": data.get("endDate") or None,
                    "contact": data.get("contact") or None,
                    "notes": data.get("notes") or None,
                }
            )
        relationships.sort(key=lambda item: item["name"].casefold())
        return relationships

    @classmethod
    def _interest_records(cls, payload: Any) -> dict[str, list[dict[str, Any]]]:
        interests = []
        for record in cls._record_list(payload, "interest"):
            data = record["data"]
            for field in ("liking", "skillLevel"):
                value = data.get(field)
                if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not math.isfinite(value) or not 0 <= value <= 100):
                    raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Interest data")
            interests.append({**cls._record_content(record), "title": record["title"],
                              "liking": data.get("liking"), "skill_level": data.get("skillLevel")})
        interests.sort(key=lambda item: item["title"].casefold())
        return {"interests": interests}

    @staticmethod
    def _knowledge_nodes(payload: Any) -> list[dict[str, Any]]:
        value = payload.get("nodes") if isinstance(payload, dict) else None
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Knowledge data")
        required = {"id", "name", "branch", "parentId", "status", "revision", "understanding", "terms"}
        if any(not required.issubset(node) for node in value):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Knowledge data")
        return [dict(node) for node in value]

    @staticmethod
    def _knowledge_by_id(nodes: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        try:
            return {int(node["id"]): node for node in nodes}
        except (TypeError, ValueError):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Knowledge data") from None

    @classmethod
    def _knowledge_path(cls, node_id: int, by_id: dict[int, dict[str, Any]]) -> list[str]:
        node = by_id.get(node_id)
        if node is None:
            raise IntegrationProviderError("not_found", "Atlas Knowledge node was not found")
        names: list[str] = []
        seen: set[int] = set()
        while node is not None:
            current_id = int(node["id"])
            if current_id in seen:
                raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Knowledge hierarchy")
            seen.add(current_id)
            names.append(str(node["name"]))
            parent_id = node.get("parentId")
            node = None if parent_id is None else by_id.get(int(parent_id))
            if parent_id is not None and node is None:
                raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Knowledge hierarchy")
        branch = str(by_id[node_id]["branch"])
        branch_name = {"subjects": "Subjects", "ideologies": "Ideologies"}.get(branch)
        if branch_name is None:
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Knowledge branch")
        return [branch_name, *reversed(names)]

    @classmethod
    def _is_descendant(cls, node_id: int, root_id: int, by_id: dict[int, dict[str, Any]]) -> bool:
        current = by_id.get(node_id)
        seen: set[int] = set()
        while current is not None and current.get("parentId") is not None:
            parent_id = int(current["parentId"])
            if parent_id == root_id:
                return True
            if parent_id in seen:
                return False
            seen.add(parent_id)
            current = by_id.get(parent_id)
        return False

    @classmethod
    def _knowledge_summary(cls, node: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
        node_id = int(node["id"])
        return {
            "node_id": node_id,
            "name": str(node["name"]),
            "status": str(node["status"]),
            "path": cls._knowledge_path(node_id, by_id),
        }

    @classmethod
    def _bounded_knowledge_node(cls, node_id: int, nodes: list[dict[str, Any]]) -> dict[str, Any]:
        by_id = cls._knowledge_by_id(nodes)
        node = by_id.get(node_id)
        if node is None:
            raise IntegrationProviderError("not_found", "Atlas Knowledge node was not found")
        parent_id = node.get("parentId")
        parent = None if parent_id is None else by_id.get(int(parent_id))
        children = sorted(
            (candidate for candidate in nodes if candidate.get("parentId") == node_id),
            key=lambda item: (str(item["name"]).casefold(), int(item["id"])),
        )
        return {
            "node_id": node_id,
            "name": str(node["name"]),
            "branch": str(node["branch"]),
            "status": str(node["status"]),
            "revision": int(node["revision"]),
            "explanation": node.get("understanding"),
            "terms": list(node.get("terms", [])),
            "path": cls._knowledge_path(node_id, by_id),
            "parent": (
                {
                    "node_id": int(parent["id"]),  # type: ignore[index]
                    "name": str(parent["name"]),
                    "status": str(parent["status"]),
                }
                if isinstance(parent, dict)
                else None
            ),
            "children": [
                {"node_id": int(child["id"]), "name": str(child["name"]), "status": str(child["status"])}
                for child in children
            ],
        }

    @classmethod
    def _knowledge_search(cls, nodes: list[dict[str, Any]], requested: dict[str, Any]) -> dict[str, Any]:
        by_id = cls._knowledge_by_id(nodes)
        query = cls._keywords(requested)
        if not query:
            raise IntegrationProviderError("invalid_input", "Knowledge search keywords cannot be blank")
        root_id = requested.get("root_node_id")
        root = by_id.get(int(root_id)) if root_id is not None else None
        if root_id is not None and root is None:
            raise IntegrationProviderError("not_found", "Atlas Knowledge root was not found")
        branch = requested.get("branch")
        if root is not None and branch is not None and root.get("branch") != branch:
            raise IntegrationProviderError("invalid_input", "Knowledge branch does not match root node")
        ranked: list[tuple[int, int, str, dict[str, Any]]] = []
        for node in nodes:
            if branch is not None and node.get("branch") != branch:
                continue
            if root is not None and not cls._is_descendant(int(node["id"]), int(root_id), by_id):
                continue
            name = str(node["name"]).casefold()
            explanation = str(node.get("understanding") or "").casefold() if node.get("status") == "known" else ""
            terms = " ".join(
                f"{term.get('label', '')} {term.get('definition', '')}"
                for term in node.get("terms", [])
                if isinstance(term, dict)
            ).casefold()
            if name == query:
                rank = 0
            elif name.startswith(query):
                rank = 1
            elif query in name:
                rank = 2
            elif query in explanation:
                rank = 3
            elif query in terms:
                rank = 4
            else:
                continue
            ranked.append((rank, len(name), name, node))
        ranked.sort(key=lambda item: (item[0], item[1], item[2], int(item[3]["id"])))
        limit = min(int(requested.get("limit", 5)), 25)
        return {"nodes": [cls._knowledge_summary(item[3], by_id) for item in ranked[:limit]]}

    @classmethod
    def _knowledge_frontier(cls, nodes: list[dict[str, Any]], requested: dict[str, Any]) -> dict[str, Any]:
        by_id = cls._knowledge_by_id(nodes)
        root_id = requested.get("root_node_id")
        root = by_id.get(int(root_id)) if root_id is not None else None
        if root_id is not None and root is None:
            raise IntegrationProviderError("not_found", "Atlas Knowledge root was not found")
        if root is not None and root.get("branch") != "subjects":
            raise IntegrationProviderError("invalid_input", "Knowledge frontier root must be a Subject")
        last_id = cls._decode_frontier_cursor(requested.get("cursor"), root_id)
        eligible = []
        for node in nodes:
            parent_id = node.get("parentId")
            parent = by_id.get(int(parent_id)) if parent_id is not None else None
            if (
                int(node["id"]) <= last_id
                or node.get("branch") != "subjects"
                or node.get("status") not in {"unknown", "unassessed"}
                or parent is None
                or parent.get("status") != "known"
                or (root is not None and not cls._is_descendant(int(node["id"]), int(root_id), by_id))
            ):
                continue
            eligible.append(node)
        eligible.sort(key=lambda item: int(item["id"]))
        limit = min(int(requested.get("limit", 50)), 100)
        page = eligible[:limit]
        next_cursor = cls._encode_frontier_cursor(int(page[-1]["id"]), root_id) if len(eligible) > limit else None
        return {"nodes": [cls._knowledge_summary(node, by_id) for node in page], "next_cursor": next_cursor}

    @staticmethod
    def _encode_frontier_cursor(last_id: int, root_id: Any) -> str:
        raw = json.dumps({"last_id": last_id, "root_node_id": root_id}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_frontier_cursor(cursor: Any, root_id: Any) -> int:
        if cursor is None:
            return 0
        try:
            encoded = str(cursor)
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            value = json.loads(raw)
            if value.get("root_node_id") != root_id or int(value["last_id"]) < 1:
                raise ValueError
            return int(value["last_id"])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise IntegrationProviderError("invalid_input", "Knowledge frontier cursor is invalid") from None


class FakeAtlasProviderAdapter:
    def __init__(self) -> None:
        self.error_type: str | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.node = {
            "node_id": 42,
            "name": "Distributed systems",
            "branch": "subjects",
            "status": "unknown",
            "revision": 3,
            "explanation": None,
            "terms": [],
            "path": ["Computer science", "Distributed systems"],
            "parent": {"node_id": 1, "name": "Computer science", "status": "known"},
            "children": [],
        }

    def execute(self, operation: IntegrationOperationSpec, input_json: dict[str, Any]) -> dict[str, Any]:
        if operation.id in ATLAS_LIFECYCLE_OPERATIONS | ATLAS_STATE_WRITE_OPERATIONS | ATLAS_ATTACHMENT_OPERATIONS:
            self.calls.append((operation.id, dict(input_json)))
            if self.error_type:
                raise IntegrationProviderError(self.error_type, "Fake Atlas operation failed")
            if not Draft202012Validator(operation.input_schema).is_valid(input_json):
                raise IntegrationProviderError("invalid_input", "Fake Atlas input is invalid")
            native = {key: value for key, value in input_json.items() if key not in {"id", "expected_revision"}}
            if operation.id == "atlas.custom_fields.list":
                return {"fields": []}
            if operation.id == "atlas.states.configuration.update":
                return {"configuration": {"revision": input_json["expected_revision"] + 1, "states": input_json["states"]}}
            if operation.id in {"atlas.states.events.create", "atlas.states.observations.create"}:
                return {"event": {"id": "fake-event", **native}, "duplicate": False}
            if operation.id == "atlas.attachments.download":
                return {"file": {"path": "workspace/downloads/fake.pdf", "filename": "fake.pdf", "bytes": 1,
                                 "sha256": "a" * 64, "media_type": "application/pdf"}}
            if operation.id == "atlas.attachments.upload":
                return {"attachment": {"id": "fake-image", "recordId": input_json["record_id"]}}
            if operation.id.endswith((".delete", ".empty")):
                return {"result": {"deleted": 0 if operation.id == "atlas.trash.empty" else True}}
            key = ("field" if operation.id.startswith("atlas.custom_fields.") else
                   "link" if operation.id.startswith("atlas.links.") else "record")
            return {key: {**native, "id": input_json.get("id", "fake-native-id"),
                          "revision": input_json.get("expected_revision", 0) + 1}}
        if operation.id in ATLAS_WRITE_OPERATIONS:
            self.calls.append((operation.id, dict(input_json)))
            if self.error_type:
                raise IntegrationProviderError(self.error_type, "Fake Atlas operation failed")
            kind = operation.id.split(".")[1]
            native = {key: value for key, value in input_json.items() if key not in {"id", "expected_revision"}}
            return {"record" if kind == "records" else "link": {
                **native, "id": input_json.get("id", "fake-native-id"),
                "revision": input_json.get("expected_revision", 0) + 1,
            }}
        if operation.id in ATLAS_READ_OPERATIONS:
            self.calls.append((operation.id, dict(input_json)))
            if self.error_type:
                raise IntegrationProviderError(self.error_type, "Fake Atlas operation failed")
            return fake_read_output(operation.id, input_json)
        operation = _transport_operation(operation)
        self.calls.append((operation.operation_id, dict(input_json)))
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Atlas operation failed")
        if operation.operation_id == "atlas.knowledge.node.get":
            return {"node": dict(self.node)}
        if operation.operation_id in {"atlas.goal.create", "atlas.goal.update"}:
            data = {"description": "", "successConditions": "", "horizon": "short",
                    "pursuitStatus": "considered", "commitment": 0, "blueprint": {"nodes": [], "edges": []}}
            UrllibAtlasProviderAdapter._patch_goal_data(data, input_json)
            nodes = data["blueprint"]["nodes"]
            goal = UrllibAtlasProviderAdapter._goal_view({
                "id": input_json.get("id", "fake-goal"), "category": "goal",
                "revision": input_json.get("expected_revision", 0) + 1,
                "title": input_json.get("title", "Fake Goal"), "data": data,
                "customFieldValues": input_json.get("custom_field_values", {}),
                "progress": sum(node.get("progress", 0) for node in nodes) / len(nodes) if nodes else None,
            })
            return {"goal": goal, **({"created": True} if operation.operation_id == "atlas.goal.create" else {})}
        return {
            "atlas.person.get": {"personal_info": None, "completeness": {}},
            "atlas.interest.get": {"interests": []},
            "atlas.interest.list": {"interests": []},
            "atlas.experience.list": {"experiences": []},
            "atlas.goal.list": {"goals": [], "graph": {"nodes": [], "edges": []}},
            "atlas.project.list": {"projects": []},
            "atlas.relationship.list": {"relationships": []},
            "atlas.knowledge.frontier.list": {"nodes": [], "next_cursor": None},
            "atlas.knowledge.search": {"nodes": []},
        }[operation.operation_id]

    def establish(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("atlas.knowledge.node.know", dict(payload)))
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Atlas establishment failed")
        node = {**self.node, "status": "known", "explanation": payload["explanation"]}
        return {"node": node, "created_children": list(payload["children"]), "existing_children": []}
