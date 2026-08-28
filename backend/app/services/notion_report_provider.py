from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from app.services.github_provider import IntegrationProviderError
from app.services.notion_todo_provider import MAX_PROVIDER_RESPONSE_BYTES, MAX_TEXT_LENGTH, NotionTodoProvider

REPORT_PROPERTY_TYPES = {
    "Name": "title",
    "Created Time": "created_time",
    "Select": "select",
}
REPORT_SELECT_OPTIONS = {
    "GitHub Projects",
    "AI News",
    "AI Research",
    "Macro",
    "Personal Feed",
}


class NotionReportProvider(NotionTodoProvider):
    """Bounded Notion Reports adapter sharing the trusted Notion transport."""

    def validate_connection(self) -> dict[str, str | None]:
        normalized_identity = self.validate_identity()
        source = self._request(
            "GET",
            f"/v1/data_sources/{quote(self.data_source_id, safe='')}",
            timeout=10,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._validate_report_schema(source)
        return normalized_identity

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
        reports: list[dict[str, Any]] = []
        for page in results:
            if not isinstance(page, dict):
                raise IntegrationProviderError("schema_mismatch", "A Notion report row is malformed")
            if self._in_trash(page):
                continue
            self._assert_contained(page)
            reports.append(self._report_from_page(page))
        has_more, next_cursor = self._pagination(response)
        return {"reports": reports, "has_more": has_more, "next_cursor": next_cursor}

    def get(
        self,
        report_id: str,
        *,
        page_size: int,
        start_cursor: str | None,
    ) -> dict[str, Any]:
        page = self._retrieve_contained(report_id)
        query: dict[str, Any] = {"page_size": page_size}
        if start_cursor is not None:
            query["start_cursor"] = start_cursor
        response = self._request(
            "GET",
            f"/v1/blocks/{quote(report_id, safe='')}/children?{urlencode(query)}",
            timeout=15,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        blocks = response.get("results")
        if (
            not isinstance(blocks, list)
            or len(blocks) > page_size
            or not all(isinstance(block, dict) for block in blocks)
        ):
            raise IntegrationProviderError("provider_unavailable", "Notion returned invalid report blocks")
        has_more, next_cursor = self._pagination(response)
        return {
            "report": self._report_from_page(page),
            "blocks": blocks,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        page = self._request(
            "POST",
            "/v1/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
                "properties": {
                    "Name": {"title": [self._text_fragment(self._bounded_report_text(values["name"]))]},
                    "Select": {"select": {"name": values["select"]}},
                },
                "children": values["children"],
            },
            timeout=15,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._assert_contained(page)
        return self._report_from_page(page)

    def delete(self, report_id: str) -> dict[str, Any]:
        self._retrieve_contained(report_id)
        page = self._request(
            "PATCH",
            f"/v1/pages/{quote(report_id, safe='')}",
            {"in_trash": True},
            timeout=15,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._assert_contained(page)
        if not self._in_trash(page):
            raise IntegrationProviderError("provider_unavailable", "Notion did not confirm report removal")
        return {"id": report_id, "removed": True}

    def _retrieve_contained(self, report_id: str) -> dict[str, Any]:
        page = self._request(
            "GET",
            f"/v1/pages/{quote(report_id, safe='')}",
            timeout=10,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._assert_contained(page)
        if self._in_trash(page):
            raise IntegrationProviderError("not_found", "The requested Notion report was not found")
        return page

    def _assert_contained(self, page: dict[str, Any]) -> None:
        parent = page.get("parent")
        actual = parent.get("data_source_id") if isinstance(parent, dict) else None
        if not isinstance(actual, str) or self._normalized_id(actual) != self._normalized_id(self.data_source_id):
            raise IntegrationProviderError("not_found", "The requested Notion report was not found")

    def _validate_report_schema(self, source: dict[str, Any]) -> None:
        properties = source.get("properties")
        if not isinstance(properties, dict):
            raise IntegrationProviderError("schema_mismatch", "The Notion report data source schema is unavailable")
        for name, expected_type in REPORT_PROPERTY_TYPES.items():
            prop = properties.get(name)
            if not isinstance(prop, dict) or prop.get("type") != expected_type:
                raise IntegrationProviderError("schema_mismatch", "The Notion report data source schema does not match")
        select = properties["Select"].get("select")
        options = select.get("options") if isinstance(select, dict) else None
        names = {item.get("name") for item in options or [] if isinstance(item, dict)}
        if names != REPORT_SELECT_OPTIONS:
            raise IntegrationProviderError("schema_mismatch", "The Notion report Select options do not match")

    def _report_from_page(self, page: dict[str, Any]) -> dict[str, Any]:
        page_id = page.get("id")
        properties = page.get("properties")
        if not isinstance(page_id, str) or not page_id or not isinstance(properties, dict):
            raise IntegrationProviderError("schema_mismatch", "A Notion report row is malformed")
        self._in_trash(page)
        for name, expected_type in REPORT_PROPERTY_TYPES.items():
            prop = properties.get(name)
            if not isinstance(prop, dict) or prop.get("type") != expected_type:
                raise IntegrationProviderError("schema_mismatch", "A Notion report row does not match the configured schema")
        name = self._report_text(properties["Name"], "title")
        created_time = properties["Created Time"].get("created_time")
        selected = properties["Select"].get("select")
        selected_name = selected.get("name") if isinstance(selected, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise IntegrationProviderError("schema_mismatch", "A Notion report name is invalid")
        if not isinstance(created_time, str):
            raise IntegrationProviderError("schema_mismatch", "A Notion report creation time is invalid")
        try:
            datetime.fromisoformat(created_time.replace("Z", "+00:00"))
        except ValueError:
            raise IntegrationProviderError("schema_mismatch", "A Notion report creation time is invalid") from None
        if selected_name not in REPORT_SELECT_OPTIONS:
            raise IntegrationProviderError("schema_mismatch", "A Notion report Select value is invalid")
        return {"id": page_id, "name": name, "created_time": created_time, "select": selected_name}

    @staticmethod
    def _report_text(prop: dict[str, Any], key: str) -> str:
        fragments = prop.get(key)
        if not isinstance(fragments, list):
            raise IntegrationProviderError("schema_mismatch", "A Notion report text property is invalid")
        parts: list[str] = []
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise IntegrationProviderError("schema_mismatch", "A Notion report text property is invalid")
            plain = fragment.get("plain_text")
            if not isinstance(plain, str):
                text = fragment.get("text")
                plain = text.get("content") if isinstance(text, dict) else None
            if not isinstance(plain, str):
                raise IntegrationProviderError("schema_mismatch", "A Notion report text property is invalid")
            parts.append(plain)
        value = "".join(parts)
        if not value.strip() or len(value) > MAX_TEXT_LENGTH:
            raise IntegrationProviderError("schema_mismatch", "A Notion report text property is invalid")
        return value

    @staticmethod
    def _bounded_report_text(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_LENGTH:
            raise IntegrationProviderError("invalid_input", "Report text input is invalid")
        return value

    @staticmethod
    def _pagination(response: dict[str, Any]) -> tuple[bool, str | None]:
        has_more = response.get("has_more")
        next_cursor = response.get("next_cursor")
        if not isinstance(has_more, bool) or (next_cursor is not None and not isinstance(next_cursor, str)):
            raise IntegrationProviderError("provider_unavailable", "Notion returned invalid pagination data")
        return has_more, next_cursor
