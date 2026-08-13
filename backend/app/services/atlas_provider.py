from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.services.github_provider import IntegrationProviderError
from app.services.integration_registry import IntegrationOperation

ATLAS_BASE_URL = "http://127.0.0.1:4817"


class AtlasProviderAdapter(Protocol):
    def validate_credential(self, credential: str) -> dict[str, str]: ...

    def execute(
        self,
        operation: IntegrationOperation,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]: ...

    def establish(self, payload: dict[str, Any], credential: str) -> dict[str, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


class UrllibAtlasProviderAdapter:
    """Fixed-route loopback adapter. Secrets exist only while a request is built."""

    _routes = {
        "atlas.person.get": "/api/agent/get_personal_info",
        "atlas.experience.list": "/api/agent/list_experiences",
        "atlas.goal.list": "/api/agent/get_goals",
        "atlas.project.list": "/api/agent/list_projects",
        "atlas.relationship.list": "/api/agent/list_relationships",
        "atlas.knowledge.frontier.list": "/api/agent/list_frontier_nodes",
        "atlas.knowledge.search": "/api/agent/search_knowledge",
        "atlas.knowledge.node.get": "/api/agent/get_knowledge_node",
    }

    def validate_credential(self, credential: str) -> dict[str, str]:
        payload = self._request("/api/agent/tools", None, credential, timeout=10, max_bytes=1_000_000, method="GET")
        if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid discovery data")
        return {"login": "Local Atlas", "id": "local-atlas"}

    def execute(
        self,
        operation: IntegrationOperation,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        route = self._routes.get(operation.operation_id)
        if route is None:
            raise IntegrationProviderError("internal_failure", "Atlas operation is unsupported")
        provider_input = input_json if operation.operation_id.startswith("atlas.knowledge.") else {}
        payload = self._request(
            route,
            provider_input,
            credential,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
            method="POST",
        )
        return self._normalize(operation.operation_id, payload, input_json)

    def establish(self, payload: dict[str, Any], credential: str) -> dict[str, Any]:
        result = self._request(
            "/api/agent/establish_known_node",
            payload,
            credential,
            timeout=20,
            max_bytes=2_000_000,
            method="POST",
        )
        if not isinstance(result, dict):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned an invalid Knowledge result")
        node = result.get("node")
        if not isinstance(node, dict):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned an invalid Knowledge result")
        return {
            "node": self._knowledge_node(node),
            "created_children": [str(item) for item in result.get("children_created", []) if isinstance(item, str)],
            "existing_children": [str(item) for item in result.get("children_existing", []) if isinstance(item, str)],
        }

    def _normalize(self, operation_id: str, payload: Any, requested: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned an invalid response")
        if operation_id == "atlas.person.get":
            return {"personal_info": payload.get("personal_info")}
        if operation_id == "atlas.experience.list":
            items = self._items(payload, "experiences")
            keywords = self._keywords(requested)
            ongoing = requested.get("ongoing")
            if keywords:
                items = [item for item in items if self._matches(item, ("title", "description"), keywords)]
            if isinstance(ongoing, bool):
                items = [item for item in items if (item.get("time") or {}).get("ongoing") is ongoing]
            return {"experiences": items[: self._limit(requested)]}
        if operation_id == "atlas.goal.list":
            goals = self._items(payload, "goals")
            importance = requested.get("importance")
            horizon = requested.get("horizon")
            if importance:
                goals = [goal for goal in goals if goal.get("importance") == importance]
            if horizon:
                goals = [goal for goal in goals if goal.get("horizon") == horizon]
            goals = goals[: self._limit(requested)]
            retained_ids = self._goal_ids(goals)
            progressions = [
                item
                for item in self._items(payload, "progressions")
                if item.get("parent_goal_id") in retained_ids
            ]
            return {"goals": goals, "progressions": progressions}
        if operation_id == "atlas.project.list":
            items = self._items(payload, "projects")
            items = self._filter_keyword_status(items, requested, ("title", "description"))
            has_link = requested.get("has_github_link")
            if isinstance(has_link, bool):
                items = [item for item in items if bool(item.get("github_link")) is has_link]
            return {"projects": items[: self._limit(requested)]}
        if operation_id == "atlas.relationship.list":
            items = self._items(payload, "relationships")
            keywords = self._keywords(requested)
            if keywords:
                items = [item for item in items if self._matches(item, ("name", "relationship_type", "notes"), keywords)]
            for requested_name, candidate_names in (
                ("kind", ("kind", "category", "relationship_type")),
                ("status", ("status",)),
                ("importance", ("importance",)),
            ):
                value = requested.get(requested_name)
                if value:
                    items = [item for item in items if any(item.get(name) == value for name in candidate_names)]
            return {"relationships": items[: self._limit(requested)]}
        if operation_id == "atlas.knowledge.frontier.list":
            nodes = [self._knowledge_summary(node) for node in self._items(payload, "nodes")]
            return {"nodes": nodes, "next_cursor": payload.get("next_cursor")}
        if operation_id == "atlas.knowledge.search":
            return {"nodes": [self._knowledge_summary(node) for node in self._items(payload, "nodes")]}
        if operation_id == "atlas.knowledge.node.get":
            node = payload.get("node", payload)
            if not isinstance(node, dict):
                raise IntegrationProviderError("provider_unavailable", "Atlas returned an invalid Knowledge node")
            return {"node": self._knowledge_node(node)}
        raise IntegrationProviderError("internal_failure", "Atlas operation is unsupported")

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None,
        credential: str,
        *,
        timeout: float,
        max_bytes: int,
        method: str,
    ) -> Any:
        raw_input = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{ATLAS_BASE_URL}{path}",
            data=raw_input,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {credential}",
            },
        )
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=timeout)
        except HTTPError as exc:
            raw_error = exc.read(65_537)
            code = self._error_code(raw_error)
            if 300 <= exc.code < 400:
                error_type = "provider_unavailable"
            elif exc.code == 401:
                error_type = "invalid_credential"
            elif exc.code == 423:
                error_type = "atlas_locked"
            elif exc.code == 404:
                error_type = "not_found"
            elif exc.code == 409:
                error_type = "node_already_known" if code == "node_already_known" else "stale_revision"
            elif exc.code == 400:
                error_type = "invalid_input"
            else:
                error_type = "provider_unavailable"
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

    @classmethod
    def _goal_ids(cls, goals: list[dict[str, Any]]) -> set[str]:
        ids: set[str] = set()
        pending = list(goals)
        while pending:
            goal = pending.pop()
            goal_id = goal.get("id")
            if isinstance(goal_id, str):
                ids.add(goal_id)
            subgoals = goal.get("subgoals")
            if isinstance(subgoals, list):
                pending.extend(item for item in subgoals if isinstance(item, dict))
        return ids

    @staticmethod
    def _knowledge_summary(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "node_id": int(node["id"]),
            "name": str(node["name"]),
            "path": [str(item) for item in node.get("path", [])],
            "status": str(node["status"]),
        }

    @classmethod
    def _knowledge_node(cls, node: dict[str, Any]) -> dict[str, Any]:
        parent = node.get("parent")
        children = node.get("children", [])
        return {
            "node_id": int(node["id"]),
            "name": str(node["name"]),
            "branch": str(node["branch"]),
            "status": str(node["status"]),
            "revision": int(node["revision"]),
            "explanation": node.get("explanation"),
            "terms": list(node.get("terms", [])),
            "path": [str(item) for item in node.get("path", [])],
            "parent": (
                {
                    "node_id": int(parent["id"]),
                    "name": str(parent["name"]),
                    "status": str(parent["status"]),
                }
                if isinstance(parent, dict)
                else None
            ),
            "children": [
                {"node_id": int(child["id"]), "name": str(child["name"]), "status": str(child["status"])}
                for child in children
                if isinstance(child, dict)
            ],
        }


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

    def validate_credential(self, credential: str) -> dict[str, str]:
        if self.error_type or credential.startswith("invalid"):
            raise IntegrationProviderError(self.error_type or "invalid_credential", "Fake Atlas validation failed")
        return {"login": "Local Atlas", "id": "local-atlas"}

    def execute(self, operation: IntegrationOperation, input_json: dict[str, Any], credential: str) -> dict[str, Any]:
        del credential
        self.calls.append((operation.operation_id, dict(input_json)))
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Atlas operation failed")
        if operation.operation_id == "atlas.knowledge.node.get":
            return {"node": dict(self.node)}
        return {
            "atlas.person.get": {"personal_info": None},
            "atlas.experience.list": {"experiences": []},
            "atlas.goal.list": {"goals": [], "progressions": []},
            "atlas.project.list": {"projects": []},
            "atlas.relationship.list": {"relationships": []},
            "atlas.knowledge.frontier.list": {"nodes": [], "next_cursor": None},
            "atlas.knowledge.search": {"nodes": []},
        }[operation.operation_id]

    def establish(self, payload: dict[str, Any], credential: str) -> dict[str, Any]:
        del credential
        self.calls.append(("atlas.knowledge.node.know", dict(payload)))
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Atlas establishment failed")
        node = {**self.node, "status": "known", "explanation": payload["explanation"]}
        return {"node": node, "created_children": list(payload["children"]), "existing_children": []}
