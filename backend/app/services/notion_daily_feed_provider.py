from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.services.github_provider import IntegrationProviderError
from app.services.notion_todo_provider import (
    MAX_PROVIDER_RESPONSE_BYTES,
    NotionTodoProvider,
)

MAX_DAILY_FEED_MARKDOWN = 20_000


class NotionDailyFeedProvider(NotionTodoProvider):
    """Bounded writer for one configured standalone Notion page."""

    def validate_connection(self) -> dict[str, str | None]:
        identity = self.validate_identity()
        page = self._request(
            "GET",
            f"/v1/pages/{quote(self.data_source_id, safe='')}",
            timeout=10,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._validate_page(page)
        return identity

    def write(self, markdown: str) -> dict[str, object]:
        bounded = self._bounded_markdown(markdown)
        response = self._request(
            "PATCH",
            f"/v1/pages/{quote(self.data_source_id, safe='')}/markdown",
            {
                "type": "replace_content",
                "replace_content": {
                    "new_str": bounded,
                    "allow_deleting_content": False,
                },
                "allow_async": False,
            },
            timeout=20,
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        self._validate_write_response(response)
        return {"updated": True, "characters": len(bounded)}

    def _validate_page(self, page: dict[str, Any]) -> None:
        page_id = page.get("id")
        archived = page.get("archived")
        in_trash = page.get("in_trash")
        if (
            page.get("object") != "page"
            or not isinstance(page_id, str)
            or self._normalized_id(page_id) != self._normalized_id(self.data_source_id)
            or ("archived" in page and not isinstance(archived, bool))
            or ("in_trash" in page and not isinstance(in_trash, bool))
        ):
            raise IntegrationProviderError(
                "schema_mismatch", "The configured Notion Daily Feed page is malformed"
            )
        if archived is True or in_trash is True:
            raise IntegrationProviderError(
                "not_found", "The configured Notion Daily Feed page was not found"
            )

    def _validate_write_response(self, response: dict[str, Any]) -> None:
        page_id = response.get("id")
        markdown = response.get("markdown")
        truncated = response.get("truncated")
        unknown_block_ids = response.get("unknown_block_ids")
        if (
            response.get("object") != "page_markdown"
            or not isinstance(page_id, str)
            or self._normalized_id(page_id) != self._normalized_id(self.data_source_id)
            or not isinstance(markdown, str)
            or not isinstance(truncated, bool)
            or not isinstance(unknown_block_ids, list)
            or len(unknown_block_ids) > 100
            or any(not isinstance(block_id, str) for block_id in unknown_block_ids)
        ):
            raise IntegrationProviderError(
                "provider_unavailable", "Notion returned an invalid Daily Feed update"
            )
        if truncated or unknown_block_ids:
            raise IntegrationProviderError(
                "provider_unavailable", "Notion did not return the complete Daily Feed update"
            )

    @staticmethod
    def _bounded_markdown(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_DAILY_FEED_MARKDOWN
        ):
            raise IntegrationProviderError(
                "invalid_input", "Daily Feed markdown must be non-empty and at most 20000 characters"
            )
        return value
