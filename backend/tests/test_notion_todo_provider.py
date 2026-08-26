import io
from urllib.error import HTTPError

import pytest

from app.services.github_provider import IntegrationProviderError
from app.services.notion_todo_provider import NOTION_VERSION, NotionTodoProvider


def schema() -> dict:
    types = {
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
    properties = {name: {"type": property_type, property_type: {}} for name, property_type in types.items()}
    properties["Priority"]["select"] = {
        "options": [{"name": "Low"}, {"name": "Medium"}, {"name": "High"}]
    }
    return {"object": "data_source", "id": "source-id", "properties": properties}


def text_property(property_type: str, value: str | None) -> dict:
    values = [] if value is None else [{"plain_text": value}]
    return {"type": property_type, property_type: values}


def page(
    *,
    page_id: str = "page-1",
    source_id: str = "source-id",
    title: str = "Todo",
    done: bool = False,
    priority: str | None = "High",
    start_at: str | None = "2026-08-21",
    due_at: str | None = "2026-08-22T12:30:00-04:00",
    estimated_minutes: int | None = 30,
    atlas_goal_id: str | None = "opaque-goal",
    notes: str | None = "Notes",
    in_trash: bool = False,
) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "created_time": "2026-08-20T10:00:00.000Z",
        "in_trash": in_trash,
        "parent": {"type": "data_source_id", "data_source_id": source_id},
        "properties": {
            "Title": text_property("title", title),
            "Done": {"type": "checkbox", "checkbox": done},
            "Priority": {"type": "select", "select": {"name": priority} if priority else None},
            "Start At": {"type": "date", "date": {"start": start_at} if start_at else None},
            "Due At": {"type": "date", "date": {"start": due_at} if due_at else None},
            "Estimated Minutes": {"type": "number", "number": estimated_minutes},
            "Atlas Goal ID": text_property("rich_text", atlas_goal_id),
            "Notes": text_property("rich_text", notes),
            "Created At": {"type": "created_time", "created_time": "2026-08-20T10:00:00.000Z"},
        },
    }


def test_validates_exact_schema_and_sanitized_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionTodoProvider("unused", "source-id")
    responses = {
        "/v1/users/me": {
            "object": "user",
            "id": "bot-id",
            "name": "Todo bot",
            "bot": {"workspace_name": "Private workspace"},
        },
        "/v1/data_sources/source-id": schema(),
    }
    monkeypatch.setattr(provider, "_request", lambda _method, path, **_kwargs: responses[path])

    assert provider.validate_connection() == {
        "bot_id": "bot-id",
        "bot_name": "Todo bot",
        "workspace_name": "Private workspace",
    }

    bad = schema()
    bad["properties"]["Notes"] = {"type": "url", "url": {}}
    responses["/v1/data_sources/source-id"] = bad
    with pytest.raises(IntegrationProviderError) as mismatch:
        provider.validate_connection()
    assert mismatch.value.error_type == "schema_mismatch"

    missing_done = schema()
    missing_done["properties"].pop("Done")
    responses["/v1/data_sources/source-id"] = missing_done
    with pytest.raises(IntegrationProviderError) as missing:
        provider.validate_connection()
    assert missing.value.error_type == "schema_mismatch"


def test_maps_nullable_properties_all_day_dates_datetimes_and_created_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = NotionTodoProvider("unused", "source-id")
    result_page = page()
    captured = {}

    def query(_method, _path, payload, **_kwargs):
        captured.update(payload)
        return {
            "results": [result_page, page(page_id="trashed", in_trash=True)],
            "has_more": True,
            "next_cursor": "cursor-2",
        }

    monkeypatch.setattr(provider, "_request", query)

    result = provider.list(page_size=2, start_cursor="cursor-1")

    assert result == {
        "todos": [
            {
                "id": "page-1",
                "title": "Todo",
                "done": False,
                "priority": "high",
                "start_at": "2026-08-21",
                "due_at": "2026-08-22T12:30:00-04:00",
                "estimated_minutes": 30,
                "atlas_goal_id": "opaque-goal",
                "notes": "Notes",
                "created_at": "2026-08-20T10:00:00.000Z",
            }
        ],
        "has_more": True,
        "next_cursor": "cursor-2",
    }
    assert captured == {
        "page_size": 2,
        "result_type": "page",
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "start_cursor": "cursor-1",
    }


def test_list_omits_provider_rejected_in_trash_query_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = NotionTodoProvider("secret-token", "source-1")
    captured: dict[str, object] = {}

    def query(_method, _path, payload, **_kwargs):
        captured.update(payload)
        return {"results": [], "has_more": False, "next_cursor": None}

    monkeypatch.setattr(provider, "_request", query)

    assert provider.list(page_size=25, start_cursor=None) == {
        "todos": [],
        "has_more": False,
        "next_cursor": None,
    }
    assert "in_trash" not in captured


@pytest.mark.parametrize("trash_status", [None, "false", 0])
def test_list_rejects_missing_or_non_boolean_trash_status(
    monkeypatch: pytest.MonkeyPatch,
    trash_status: object,
) -> None:
    provider = NotionTodoProvider("secret-token", "source-1")
    malformed = page()
    if trash_status is None:
        malformed.pop("in_trash")
    else:
        malformed["in_trash"] = trash_status
    monkeypatch.setattr(
        provider,
        "_request",
        lambda *_args, **_kwargs: {
            "results": [malformed],
            "has_more": False,
            "next_cursor": None,
        },
    )

    with pytest.raises(IntegrationProviderError) as mismatch:
        provider.list(page_size=25, start_cursor=None)
    assert mismatch.value.error_type == "schema_mismatch"


def test_delete_uses_page_update_trash_field_and_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = NotionTodoProvider("secret-token", "source-1")
    calls: list[tuple[str, str, dict | None]] = []

    def request(method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        return page(source_id="source-1", in_trash=method == "PATCH")

    monkeypatch.setattr(provider, "_request", request)

    assert provider.delete("page-1") == {"id": "page-1", "removed": True}
    assert calls == [
        ("GET", "/v1/pages/page-1", None),
        ("PATCH", "/v1/pages/page-1", {"in_trash": True}),
    ]


def test_title_only_create_and_partial_update_with_explicit_clearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = NotionTodoProvider("unused", "source-id")
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        if method == "GET":
            return page()
        if path == "/v1/pages":
            return page(title="Only a title", priority=None, start_at=None, due_at=None, estimated_minutes=None, atlas_goal_id=None, notes=None)
        return page(done=True, priority=None, notes=None)

    monkeypatch.setattr(provider, "_request", fake_request)
    created = provider.create({"title": "Only a title"})
    updated = provider.update("page-1", {"done": True, "priority": None, "notes": None})

    assert created["title"] == "Only a title"
    assert calls[0] == (
        "POST",
        "/v1/pages",
        {
            "parent": {"type": "data_source_id", "data_source_id": "source-id"},
            "properties": {
                "Title": {"title": [{"type": "text", "text": {"content": "Only a title"}}]}
            },
        },
    )
    assert updated["priority"] is None
    assert updated["done"] is True
    assert calls[2][2] == {
        "properties": {
            "Done": {"checkbox": True},
            "Priority": {"select": None},
            "Notes": {"rich_text": []},
        }
    }


def test_update_and_delete_reject_pages_outside_configured_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = NotionTodoProvider("unused", "source-id")
    monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: page(source_id="other-source"))

    for action in (
        lambda: provider.update("page-1", {"title": "Changed"}),
        lambda: provider.delete("page-1"),
    ):
        with pytest.raises(IntegrationProviderError) as outside:
            action()
        assert outside.value.error_type == "not_found"


def test_malformed_manual_rows_fail_with_schema_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionTodoProvider("unused", "source-id")
    malformed = page()
    malformed["properties"]["Estimated Minutes"]["number"] = 2.5
    monkeypatch.setattr(
        provider,
        "_request",
        lambda *_args, **_kwargs: {"results": [malformed], "has_more": False, "next_cursor": None},
    )

    with pytest.raises(IntegrationProviderError) as mismatch:
        provider.list(page_size=25, start_cursor=None)
    assert mismatch.value.error_type == "schema_mismatch"
    assert "2.5" not in str(mismatch.value)


def test_non_boolean_done_status_fails_with_schema_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionTodoProvider("unused", "source-id")
    malformed = page()
    malformed["properties"]["Done"]["checkbox"] = "false"
    monkeypatch.setattr(
        provider,
        "_request",
        lambda *_args, **_kwargs: {"results": [malformed], "has_more": False, "next_cursor": None},
    )

    with pytest.raises(IntegrationProviderError) as mismatch:
        provider.list(page_size=25, start_cursor=None)
    assert mismatch.value.error_type == "schema_mismatch"


def test_fixed_host_version_headers_redirect_bounds_and_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = NotionTodoProvider("secret-token", "source-id")
    captured = {}

    class Response:
        status = 200

        def read(self, _limit):
            return b'{"ok":true}'

        def close(self):
            return None

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr("app.services.notion_todo_provider.build_opener", lambda *_args: Opener())
    assert provider._request("GET", "/v1/users/me", timeout=7, max_bytes=100) == {"ok": True}
    request = captured["request"]
    headers = dict(request.header_items())
    assert request.full_url == "https://api.notion.com/v1/users/me"
    assert headers["Notion-version"] == NOTION_VERSION
    assert headers["Authorization"] == "Bearer secret-token"
    assert captured["timeout"] == 7

    class RedirectOpener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 302, "Found", {}, io.BytesIO(b""))

    monkeypatch.setattr("app.services.notion_todo_provider.build_opener", lambda *_args: RedirectOpener())
    with pytest.raises(IntegrationProviderError) as redirect:
        provider._request("GET", "/v1/users/me", timeout=7, max_bytes=100)
    assert redirect.value.error_type == "provider_unavailable"

    class RateOpener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 429, "Limited", {"Retry-After": "12"}, io.BytesIO(b""))

    monkeypatch.setattr("app.services.notion_todo_provider.build_opener", lambda *_args: RateOpener())
    with pytest.raises(IntegrationProviderError) as limited:
        provider._request("GET", "/v1/users/me", timeout=7, max_bytes=100)
    assert limited.value.error_type == "rate_limited"
    assert limited.value.retry_after_seconds == 12

    class LargeResponse(Response):
        def read(self, limit):
            return b"x" * limit

    class LargeOpener:
        def open(self, request, timeout):
            return LargeResponse()

    monkeypatch.setattr("app.services.notion_todo_provider.build_opener", lambda *_args: LargeOpener())
    with pytest.raises(IntegrationProviderError) as large:
        provider._request("GET", "/v1/users/me", timeout=7, max_bytes=100)
    assert large.value.error_type == "response_too_large"

    class TimeoutOpener:
        def open(self, request, timeout):
            raise TimeoutError

    monkeypatch.setattr("app.services.notion_todo_provider.build_opener", lambda *_args: TimeoutOpener())
    with pytest.raises(IntegrationProviderError) as timed_out:
        provider._request("GET", "/v1/users/me", timeout=7, max_bytes=100)
    assert timed_out.value.error_type == "provider_timeout"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(400, "invalid_input"), (401, "invalid_credential"), (403, "provider_forbidden"), (404, "not_found")],
)
def test_http_errors_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected: str,
) -> None:
    provider = NotionTodoProvider("secret-token", "source-id")

    class ErrorOpener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, status_code, "provider detail", {}, io.BytesIO(b"sensitive"))

    monkeypatch.setattr("app.services.notion_todo_provider.build_opener", lambda *_args: ErrorOpener())
    with pytest.raises(IntegrationProviderError) as normalized:
        provider._request("GET", "/v1/users/me", timeout=7, max_bytes=100)
    assert normalized.value.error_type == expected
    assert "provider detail" not in str(normalized.value)
    assert "sensitive" not in str(normalized.value)
