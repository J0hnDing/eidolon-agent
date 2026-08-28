from __future__ import annotations

from typing import Any

import pytest

from app.services.github_provider import IntegrationProviderError
from app.services.notion_report_provider import (
    REPORT_SELECT_OPTIONS,
    NotionReportProvider,
)


def schema() -> dict[str, Any]:
    return {
        "object": "data_source",
        "id": "report-source-id",
        "properties": {
            "Name": {"type": "title", "title": {}},
            "Created Time": {"type": "created_time", "created_time": {}},
            "Select": {
                "type": "select",
                "select": {"options": [{"name": name} for name in sorted(REPORT_SELECT_OPTIONS)]},
            },
        },
    }


def page(
    *,
    page_id: str = "report-1",
    source_id: str = "report-source-id",
    name: str = "Weekly report",
    selected: str = "GitHub Projects",
    in_trash: bool = False,
) -> dict[str, Any]:
    return {
        "object": "page",
        "id": page_id,
        "in_trash": in_trash,
        "parent": {"type": "data_source_id", "data_source_id": source_id},
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": name}]},
            "Created Time": {
                "type": "created_time",
                "created_time": "2026-08-31T12:00:00.000Z",
            },
            "Select": {"type": "select", "select": {"name": selected}},
        },
    }


def metadata() -> dict[str, Any]:
    return {
        "id": "report-1",
        "name": "Weekly report",
        "created_time": "2026-08-31T12:00:00.000Z",
        "select": "GitHub Projects",
    }


def test_validates_exact_report_schema_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionReportProvider("unused", "report-source-id")
    responses = {
        "/v1/users/me": {
            "object": "user",
            "id": "bot-id",
            "name": "Reports bot",
            "bot": {"workspace_name": "Private workspace"},
        },
        "/v1/data_sources/report-source-id": schema(),
    }
    monkeypatch.setattr(provider, "_request", lambda _method, path, **_kwargs: responses[path])

    assert provider.validate_connection() == {
        "bot_id": "bot-id",
        "bot_name": "Reports bot",
        "workspace_name": "Private workspace",
    }

    invalid = schema()
    invalid["properties"]["Select"]["select"]["options"].pop()
    responses["/v1/data_sources/report-source-id"] = invalid
    with pytest.raises(IntegrationProviderError) as mismatch:
        provider.validate_connection()
    assert mismatch.value.error_type == "schema_mismatch"


def test_lists_only_active_contained_reports_with_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionReportProvider("unused", "report-source-id")
    captured = {}

    def query(_method, _path, payload, **_kwargs):
        captured.update(payload)
        return {
            "results": [page(), page(page_id="trashed", in_trash=True)],
            "has_more": True,
            "next_cursor": "cursor-2",
        }

    monkeypatch.setattr(provider, "_request", query)

    assert provider.list(page_size=2, start_cursor="cursor-1") == {
        "reports": [metadata()],
        "has_more": True,
        "next_cursor": "cursor-2",
    }
    assert captured == {
        "page_size": 2,
        "result_type": "page",
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "start_cursor": "cursor-1",
    }


def test_get_returns_raw_paginated_top_level_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionReportProvider("unused", "report-source-id")
    blocks = [
        {"object": "block", "id": "block-1", "type": "heading_1", "heading_1": {"rich_text": []}},
        {"object": "block", "id": "block-2", "type": "unsupported", "unsupported": {"block_type": "button"}},
    ]
    paths = []

    def request(method, path, **_kwargs):
        paths.append((method, path))
        if path.startswith("/v1/pages/"):
            return page()
        return {"results": blocks, "has_more": False, "next_cursor": None}

    monkeypatch.setattr(provider, "_request", request)

    assert provider.get("report-1", page_size=25, start_cursor="opaque cursor") == {
        "report": metadata(),
        "blocks": blocks,
        "has_more": False,
        "next_cursor": None,
    }
    assert paths == [
        ("GET", "/v1/pages/report-1"),
        ("GET", "/v1/blocks/report-1/children?page_size=25&start_cursor=opaque+cursor"),
    ]


def test_create_passes_raw_children_without_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionReportProvider("unused", "report-source-id")
    children = [{"type": "divider", "divider": {}}, {"type": "bookmark", "bookmark": {"url": "https://example.com"}}]
    captured = {}

    def create(method, path, payload, **_kwargs):
        captured.update({"method": method, "path": path, "payload": payload})
        return page()

    monkeypatch.setattr(provider, "_request", create)

    assert provider.create({"name": "Weekly report", "select": "GitHub Projects", "children": children}) == metadata()
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/pages"
    assert captured["payload"]["parent"] == {
        "type": "data_source_id",
        "data_source_id": "report-source-id",
    }
    assert captured["payload"]["children"] is children
    assert captured["payload"]["properties"]["Select"] == {
        "select": {"name": "GitHub Projects"}
    }


def test_get_and_delete_enforce_containment_and_confirm_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionReportProvider("unused", "report-source-id")
    monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: page(source_id="other-source"))
    with pytest.raises(IntegrationProviderError) as outside:
        provider.get("report-1", page_size=25, start_cursor=None)
    assert outside.value.error_type == "not_found"

    responses = iter([page(), page(in_trash=True)])
    monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: next(responses))
    assert provider.delete("report-1") == {"id": "report-1", "removed": True}


def test_malformed_block_pagination_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionReportProvider("unused", "report-source-id")
    responses = iter([page(), {"results": ["not-a-block"], "has_more": False, "next_cursor": None}])
    monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(IntegrationProviderError) as malformed:
        provider.get("report-1", page_size=25, start_cursor=None)
    assert malformed.value.error_type == "provider_unavailable"
