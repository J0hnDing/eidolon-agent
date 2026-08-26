from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.services.github_provider import IntegrationProviderError

NOTION_API_BASE = "https://api.notion.com"
NOTION_VERSION = "2026-03-11"
MAX_TEXT_LENGTH = 2_000
MAX_PROVIDER_RESPONSE_BYTES = 2_000_000

PROPERTY_TYPES = {
    "Title": "title",
    "Done": "checkbox",
    "Priority": "select",
    "Start At": "date",
    "Due At": "date",
    "Estimated Minutes": "number",
    "Atlas Goal ID": "rich_text",
    "Notes": "rich_text",
    "Created At": "created_time",
}
PRIORITY_TO_NOTION = {"low": "Low", "medium": "Medium", "high": "High"}
PRIORITY_FROM_NOTION = {value: key for key, value in PRIORITY_TO_NOTION.items()}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


class NotionTodoProvider:
    def __init__(self, token: str, data_source_id: str) -> None:
        self._token = token
        self.data_source_id = data_source_id

    def validate_connection(self) -> dict[str, str | None]:
        identity = self._request("GET", "/v1/users/me", timeout=10, max_bytes=500_000)
        source = self._request(
            "GET",
            f"/v1/data_sources/{quote(self.data_source_id, safe='')}",
            timeout=10,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._validate_schema(source)
        bot_id = identity.get("id")
        bot_name = identity.get("name")
        bot = identity.get("bot") if isinstance(identity.get("bot"), dict) else {}
        workspace_name = bot.get("workspace_name")
        if not isinstance(bot_id, str) or not bot_id:
            raise IntegrationProviderError("provider_unavailable", "Notion returned an invalid bot identity")
        return {
            "bot_id": bot_id,
            "bot_name": bot_name if isinstance(bot_name, str) and bot_name else None,
            "workspace_name": workspace_name if isinstance(workspace_name, str) and workspace_name else None,
        }

    def list(self, *, page_size: int, start_cursor: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "page_size": page_size,
            "result_type": "page",
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        }
        if start_cursor is not None:
            payload["start_cursor"] = start_cursor
        response = self._request(
            "POST",
            f"/v1/data_sources/{quote(self.data_source_id, safe='')}/query",
            payload,
            timeout=15,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        results = response.get("results")
        if not isinstance(results, list) or len(results) > page_size:
            raise IntegrationProviderError("provider_unavailable", "Notion returned invalid pagination data")
        todos = []
        for page in results:
            if not isinstance(page, dict):
                raise IntegrationProviderError("schema_mismatch", "A Notion todo row is malformed")
            if self._in_trash(page):
                continue
            self._assert_contained(page)
            todos.append(self._todo_from_page(page))
        next_cursor = response.get("next_cursor")
        has_more = response.get("has_more")
        if not isinstance(has_more, bool) or (next_cursor is not None and not isinstance(next_cursor, str)):
            raise IntegrationProviderError("provider_unavailable", "Notion returned invalid pagination data")
        return {"todos": todos, "has_more": has_more, "next_cursor": next_cursor}

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        page = self._request(
            "POST",
            "/v1/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
                "properties": self._properties_for_write(values),
            },
            timeout=15,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._assert_contained(page)
        return self._todo_from_page(page)

    def update(self, todo_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self._retrieve_contained(todo_id)
        page = self._request(
            "PATCH",
            f"/v1/pages/{quote(todo_id, safe='')}",
            {"properties": self._properties_for_write(changes)},
            timeout=15,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._assert_contained(page)
        return self._todo_from_page(page)

    def delete(self, todo_id: str) -> dict[str, Any]:
        self._retrieve_contained(todo_id)
        page = self._request(
            "PATCH",
            f"/v1/pages/{quote(todo_id, safe='')}",
            {"in_trash": True},
            timeout=15,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._assert_contained(page)
        if not self._in_trash(page):
            raise IntegrationProviderError("provider_unavailable", "Notion did not confirm todo removal")
        return {"id": todo_id, "removed": True}

    def _retrieve_contained(self, todo_id: str) -> dict[str, Any]:
        page = self._request(
            "GET",
            f"/v1/pages/{quote(todo_id, safe='')}",
            timeout=10,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._assert_contained(page)
        return page

    def _assert_contained(self, page: dict[str, Any]) -> None:
        parent = page.get("parent")
        actual = parent.get("data_source_id") if isinstance(parent, dict) else None
        if not isinstance(actual, str) or self._normalized_id(actual) != self._normalized_id(self.data_source_id):
            raise IntegrationProviderError("not_found", "The requested Notion todo was not found")

    @staticmethod
    def _normalized_id(value: str) -> str:
        return value.replace("-", "").lower()

    def _validate_schema(self, source: dict[str, Any]) -> None:
        properties = source.get("properties")
        if not isinstance(properties, dict):
            raise IntegrationProviderError("schema_mismatch", "The Notion data source schema is unavailable")
        for name, expected_type in PROPERTY_TYPES.items():
            prop = properties.get(name)
            if not isinstance(prop, dict) or prop.get("type") != expected_type:
                raise IntegrationProviderError("schema_mismatch", "The Notion todo data source schema does not match")
        priority = properties["Priority"].get("select")
        options = priority.get("options") if isinstance(priority, dict) else None
        names = {item.get("name") for item in options or [] if isinstance(item, dict)}
        if names != set(PRIORITY_FROM_NOTION):
            raise IntegrationProviderError("schema_mismatch", "The Notion Priority options do not match")

    def _todo_from_page(self, page: dict[str, Any]) -> dict[str, Any]:
        page_id = page.get("id")
        created_at = page.get("created_time")
        properties = page.get("properties")
        if not isinstance(page_id, str) or not page_id or not isinstance(properties, dict):
            raise IntegrationProviderError("schema_mismatch", "A Notion todo row is malformed")
        self._in_trash(page)
        self._validate_created_at(created_at)
        self._require_page_property_types(properties)
        title = self._text_value(properties["Title"], "title", required=True)
        priority_value = properties["Priority"].get("select")
        if priority_value is None:
            priority = None
        elif isinstance(priority_value, dict) and priority_value.get("name") in PRIORITY_FROM_NOTION:
            priority = PRIORITY_FROM_NOTION[str(priority_value["name"])]
        else:
            raise IntegrationProviderError("schema_mismatch", "A Notion todo priority is invalid")
        estimated = properties["Estimated Minutes"].get("number")
        if estimated is not None and (
            not isinstance(estimated, int) or isinstance(estimated, bool) or estimated <= 0
        ):
            raise IntegrationProviderError("schema_mismatch", "A Notion todo estimate is invalid")
        done = properties["Done"].get("checkbox")
        if not isinstance(done, bool):
            raise IntegrationProviderError("schema_mismatch", "A Notion todo done status is invalid")
        return {
            "id": page_id,
            "title": title,
            "done": done,
            "priority": priority,
            "start_at": self._date_value(properties["Start At"]),
            "due_at": self._date_value(properties["Due At"]),
            "estimated_minutes": estimated,
            "atlas_goal_id": self._text_value(properties["Atlas Goal ID"], "rich_text"),
            "notes": self._text_value(properties["Notes"], "rich_text"),
            "created_at": created_at,
        }

    @staticmethod
    def _in_trash(page: dict[str, Any]) -> bool:
        value = page.get("in_trash")
        if not isinstance(value, bool):
            raise IntegrationProviderError("schema_mismatch", "A Notion todo trash status is invalid")
        return value

    @staticmethod
    def _require_page_property_types(properties: dict[str, Any]) -> None:
        for name, expected_type in PROPERTY_TYPES.items():
            prop = properties.get(name)
            if not isinstance(prop, dict) or prop.get("type") != expected_type:
                raise IntegrationProviderError("schema_mismatch", "A Notion todo row does not match the configured schema")

    @staticmethod
    def _validate_created_at(value: Any) -> None:
        if not isinstance(value, str):
            raise IntegrationProviderError("schema_mismatch", "A Notion todo creation time is invalid")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise IntegrationProviderError("schema_mismatch", "A Notion todo creation time is invalid") from None

    @staticmethod
    def _date_value(prop: dict[str, Any]) -> str | None:
        value = prop.get("date")
        if value is None:
            return None
        start = value.get("start") if isinstance(value, dict) else None
        if not isinstance(start, str):
            raise IntegrationProviderError("schema_mismatch", "A Notion todo date is invalid")
        NotionTodoProvider._validate_date_input(start, error_type="schema_mismatch")
        return start

    @staticmethod
    def _text_value(prop: dict[str, Any], key: str, *, required: bool = False) -> str | None:
        fragments = prop.get(key)
        if not isinstance(fragments, list):
            raise IntegrationProviderError("schema_mismatch", "A Notion todo text property is invalid")
        parts: list[str] = []
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise IntegrationProviderError("schema_mismatch", "A Notion todo text property is invalid")
            plain = fragment.get("plain_text")
            if not isinstance(plain, str):
                text = fragment.get("text")
                plain = text.get("content") if isinstance(text, dict) else None
            if not isinstance(plain, str):
                raise IntegrationProviderError("schema_mismatch", "A Notion todo text property is invalid")
            parts.append(plain)
        value = "".join(parts)
        if len(value) > MAX_TEXT_LENGTH or (required and not value.strip()):
            raise IntegrationProviderError("schema_mismatch", "A Notion todo text property is invalid")
        return value if value else None

    @classmethod
    def _properties_for_write(cls, values: dict[str, Any]) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        if "title" in values:
            title = cls._bounded_text(values["title"], required=True)
            properties["Title"] = {"title": [cls._text_fragment(title)]}
        if "done" in values:
            properties["Done"] = {"checkbox": values["done"]}
        if "priority" in values:
            priority = values["priority"]
            properties["Priority"] = {
                "select": {"name": PRIORITY_TO_NOTION[priority]} if priority is not None else None
            }
        for field, notion_name in (("start_at", "Start At"), ("due_at", "Due At")):
            if field in values:
                value = values[field]
                if value is not None:
                    cls._validate_date_input(value)
                properties[notion_name] = {"date": {"start": value} if value is not None else None}
        if "estimated_minutes" in values:
            properties["Estimated Minutes"] = {"number": values["estimated_minutes"]}
        for field, notion_name in (("atlas_goal_id", "Atlas Goal ID"), ("notes", "Notes")):
            if field in values:
                value = values[field]
                if value is None:
                    properties[notion_name] = {"rich_text": []}
                else:
                    properties[notion_name] = {"rich_text": [cls._text_fragment(cls._bounded_text(value))]}
        return properties

    @staticmethod
    def _bounded_text(value: Any, *, required: bool = False) -> str:
        if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH or (required and not value.strip()):
            raise IntegrationProviderError("invalid_input", "Todo text input is invalid")
        return value

    @staticmethod
    def _text_fragment(value: str) -> dict[str, Any]:
        return {"type": "text", "text": {"content": value}}

    @staticmethod
    def _validate_date_input(value: Any, *, error_type: str = "invalid_input") -> None:
        if not isinstance(value, str):
            raise IntegrationProviderError(error_type, "Todo date input is invalid")
        try:
            if "T" in value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            else:
                date.fromisoformat(value)
        except ValueError:
            raise IntegrationProviderError(error_type, "Todo date input is invalid") from None

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float,
        max_bytes: int,
    ) -> dict[str, Any]:
        url = f"{NOTION_API_BASE}{path}"
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.notion.com" or not parsed.path.startswith("/v1/"):
            raise IntegrationProviderError("internal_failure", "Notion provider URL is outside the trusted boundary")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": NOTION_VERSION,
                "User-Agent": "eidolon-notion-integration",
            },
        )
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=timeout)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise IntegrationProviderError("provider_unavailable", "Notion redirects are not accepted") from None
            mapping = {
                400: ("invalid_input", "Notion rejected the todo request"),
                401: ("invalid_credential", "The Notion credential is invalid or revoked"),
                403: ("provider_forbidden", "Notion denied the requested todo operation"),
                404: ("not_found", "The requested Notion resource was not found"),
                429: ("rate_limited", "Notion rate limited the integration request"),
                529: ("rate_limited", "Notion rate limited the integration request"),
            }
            error_type, message = mapping.get(
                exc.code,
                ("provider_unavailable", "Notion could not complete the integration request"),
            )
            retry_after = self._retry_after(exc.headers.get("Retry-After")) if exc.code in {429, 529} else None
            raise IntegrationProviderError(error_type, message, retry_after_seconds=retry_after) from None
        except TimeoutError:
            raise IntegrationProviderError("provider_timeout", "Notion did not respond before the timeout") from None
        except (OSError, URLError):
            raise IntegrationProviderError("provider_unavailable", "Notion is unavailable") from None
        try:
            if 300 <= int(response.status) < 400:
                raise IntegrationProviderError("provider_unavailable", "Notion redirects are not accepted")
            raw = response.read(max_bytes + 1)
        finally:
            response.close()
        if len(raw) > max_bytes:
            raise IntegrationProviderError("response_too_large", "Notion response exceeded the size limit")
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise IntegrationProviderError("provider_unavailable", "Notion returned an invalid response") from None
        if not isinstance(decoded, dict):
            raise IntegrationProviderError("provider_unavailable", "Notion returned an invalid response")
        return decoded

    @staticmethod
    def _retry_after(value: Any) -> int | None:
        try:
            seconds = int(str(value))
        except (TypeError, ValueError):
            return None
        return seconds if 0 <= seconds <= 86_400 else None
