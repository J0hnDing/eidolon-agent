from __future__ import annotations

import base64
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
        if route is not None:
            payload = self._request(
                route,
                {},
                credential,
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
                method="POST",
            )
        elif operation.operation_id == "atlas.relationship.list":
            payload = self._request(
                "/api/records?category=relationship",
                None,
                credential,
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
                method="GET",
            )
        elif operation.operation_id.startswith("atlas.knowledge."):
            payload = self._request(
                "/api/knowledge/nodes",
                None,
                credential,
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
                method="GET",
            )
        else:
            raise IntegrationProviderError("internal_failure", "Atlas operation is unsupported")
        return self._normalize(operation.operation_id, payload, input_json)

    def establish(self, payload: dict[str, Any], credential: str) -> dict[str, Any]:
        nodes = self._knowledge_nodes(
            self._request(
                "/api/knowledge/nodes", None, credential, timeout=10, max_bytes=2_000_000, method="GET"
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
            credential,
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
                credential,
                timeout=20,
                max_bytes=2_000_000,
                method="POST",
            )
            existing_by_name[key] = child_name
            created_children.append(child_name)
        refreshed = self._knowledge_nodes(
            self._request(
                "/api/knowledge/nodes", None, credential, timeout=10, max_bytes=2_000_000, method="GET"
            )
        )
        final_node = self._bounded_knowledge_node(int(target["id"]), refreshed)
        return {
            "node": final_node,
            "created_children": created_children,
            "existing_children": existing_children,
        }

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

    @classmethod
    def _relationship_records(cls, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid Relationship data")
        relationships = []
        for record in payload:
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            relationships.append(
                {
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
