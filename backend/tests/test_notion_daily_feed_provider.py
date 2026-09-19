from __future__ import annotations

import pytest

from app.services.github_provider import IntegrationProviderError
from app.services.notion_daily_feed_provider import NotionDailyFeedProvider
from app.services.notion_todo_provider import MAX_PROVIDER_RESPONSE_BYTES


def _page(*, page_id: str = "daily-page", archived: bool = False, in_trash: bool = False):
    return {
        "object": "page",
        "id": page_id,
        "archived": archived,
        "in_trash": in_trash,
    }


def _identity():
    return {
        "object": "user",
        "id": "bot-id",
        "name": "Daily Feed bot",
        "bot": {"workspace_name": "Private workspace"},
    }


def _write_response(*, page_id: str = "daily-page", truncated: bool = False, unknown=None):
    return {
        "object": "page_markdown",
        "id": page_id,
        "markdown": "# Daily Feed\n",
        "truncated": truncated,
        "unknown_block_ids": [] if unknown is None else unknown,
    }


def test_validates_shared_active_page_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionDailyFeedProvider("unused", "daily-page")
    responses = {
        "/v1/users/me": _identity(),
        "/v1/pages/daily-page": _page(),
    }
    monkeypatch.setattr(provider, "_request", lambda _method, path, **_kwargs: responses[path])

    assert provider.validate_connection() == {
        "bot_id": "bot-id",
        "bot_name": "Daily Feed bot",
        "workspace_name": "Private workspace",
    }

    responses["/v1/pages/daily-page"] = _page(in_trash=True)
    with pytest.raises(IntegrationProviderError) as error:
        provider.validate_connection()
    assert error.value.error_type == "not_found"


def test_validates_title_only_standalone_page(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = NotionDailyFeedProvider("unused", "daily-page")
    responses = {
        "/v1/users/me": _identity(),
        "/v1/pages/daily-page": {
            "object": "page",
            "id": "daily-page",
            "properties": {
                "title": {
                    "id": "title",
                    "type": "title",
                    "title": [{"type": "text", "plain_text": "Daily Feed"}],
                }
            },
        },
    }
    monkeypatch.setattr(provider, "_request", lambda _method, path, **_kwargs: responses[path])

    assert provider.validate_connection()["bot_id"] == "bot-id"


@pytest.mark.parametrize("field", ["archived", "in_trash"])
def test_rejects_malformed_optional_page_state(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    provider = NotionDailyFeedProvider("unused", "daily-page")
    malformed_page = {"object": "page", "id": "daily-page", field: None}
    responses = {
        "/v1/users/me": _identity(),
        "/v1/pages/daily-page": malformed_page,
    }
    monkeypatch.setattr(provider, "_request", lambda _method, path, **_kwargs: responses[path])

    with pytest.raises(IntegrationProviderError) as error:
        provider.validate_connection()
    assert error.value.error_type == "schema_mismatch"


def test_write_replaces_only_the_configured_page_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = NotionDailyFeedProvider("unused", "daily-page")
    captured = {}

    def request(method, path, payload, **kwargs):
        captured.update(
            {"method": method, "path": path, "payload": payload, "kwargs": kwargs}
        )
        return _write_response()

    monkeypatch.setattr(provider, "_request", request)

    assert provider.write("# Daily Feed\n\nConcise.") == {
        "updated": True,
        "characters": 22,
    }
    assert captured == {
        "method": "PATCH",
        "path": "/v1/pages/daily-page/markdown",
        "payload": {
            "type": "replace_content",
            "replace_content": {
                "new_str": "# Daily Feed\n\nConcise.",
                "allow_deleting_content": False,
            },
            "allow_async": False,
        },
        "kwargs": {"timeout": 20, "max_bytes": MAX_PROVIDER_RESPONSE_BYTES},
    }


@pytest.mark.parametrize(
    "response",
    [
        _write_response(page_id="other-page"),
        _write_response(truncated=True),
        _write_response(unknown=["unknown-block"]),
        {"object": "async_task", "id": "task"},
    ],
)
def test_write_rejects_unconfirmed_or_incomplete_updates(
    monkeypatch: pytest.MonkeyPatch,
    response,
) -> None:
    provider = NotionDailyFeedProvider("unused", "daily-page")
    monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: response)

    with pytest.raises(IntegrationProviderError) as error:
        provider.write("# Daily Feed")
    assert error.value.error_type == "provider_unavailable"


@pytest.mark.parametrize("markdown", ["", "   ", "x" * 20_001])
def test_write_rejects_invalid_markdown(markdown: str) -> None:
    provider = NotionDailyFeedProvider("unused", "daily-page")
    with pytest.raises(IntegrationProviderError) as error:
        provider.write(markdown)
    assert error.value.error_type == "invalid_input"
