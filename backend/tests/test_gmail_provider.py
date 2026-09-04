import base64
import json
from email.parser import BytesParser
from email.policy import default
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services import gmail_provider as provider_module
from app.services.gmail_provider import (
    GMAIL_OAUTH_REDIRECT_URI,
    GMAIL_SCOPE,
    READ_NEW_QUERY,
    UrllibGmailProviderAdapter,
)
from app.services.google_oauth import PendingGoogleOAuth
from app.services.integration_registry import OPERATIONS


def credential() -> str:
    return json.dumps(
        {"client_id": "gmail-client", "client_secret": "gmail-secret", "refresh_token": "gmail-refresh"}
    )


def test_http_opener_is_lazy_for_non_network_status_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls = 0

    def build_opener(*_args):
        nonlocal build_calls
        build_calls += 1
        return object()

    monkeypatch.setattr(provider_module, "build_opener", build_opener)
    adapter = UrllibGmailProviderAdapter()

    adapter.authorization_url("client-id", "state-value")

    assert build_calls == 0


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def gmail_message(message_id: str = "message-1", *, unread: bool = True, html_only: bool = False) -> dict:
    text_part = {
        "mimeType": "text/html" if html_only else "text/plain",
        "filename": "",
        "body": {"data": encoded("<p>Hello <b>there</b></p>" if html_only else "Hello there"), "size": 11},
    }
    return {
        "id": message_id,
        "threadId": "thread-1",
        "labelIds": ["INBOX", *(("UNREAD",) if unread else ())],
        "snippet": "Hello there",
        "payload": {
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": [
                {"name": "From", "value": "Sender <sender@example.com>"},
                {"name": "To", "value": "person@example.com"},
                {"name": "Subject", "value": "Quarterly report"},
                {"name": "Date", "value": "Fri, 28 Aug 2026 12:00:00 +0000"},
            ],
            "body": {"size": 0},
            "parts": [
                text_part,
                {
                    "mimeType": "application/pdf",
                    "filename": "report.pdf",
                    "body": {"attachmentId": "attachment-secret", "size": 1234, "data": encoded("private")},
                },
            ],
        },
    }


def bypass_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.gmail_provider.refresh_google_access_token", lambda *_args: "access-token")


def test_gmail_oauth_is_offline_scoped_and_requires_account_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibGmailProviderAdapter()
    query = parse_qs(urlsplit(adapter.authorization_url("client-id", "state-value")).query)
    assert query["redirect_uri"] == [GMAIL_OAUTH_REDIRECT_URI]
    assert query["scope"] == [f"openid email {GMAIL_SCOPE}"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent select_account"]

    monkeypatch.setattr(
        adapter,
        "_request_json",
        lambda *_args, **_kwargs: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "scope": f"openid email {GMAIL_SCOPE}",
        },
    )
    assert adapter.exchange_code(PendingGoogleOAuth("client-id", "client-secret", 100), "code") == {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }


def test_search_and_conversation_normalize_without_attachment_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UrllibGmailProviderAdapter()
    bypass_refresh(monkeypatch)
    calls: list[str] = []
    responses = iter(
        [
            {"threads": [{"id": "thread-1"}], "nextPageToken": "next"},
            {"id": "thread-1", "messages": [gmail_message()]},
            {"id": "thread-1", "messages": [gmail_message(html_only=True)]},
        ]
    )

    def request_json(url: str, **_kwargs):
        calls.append(url)
        return next(responses)

    monkeypatch.setattr(adapter, "_request_json", request_json)
    searched = adapter.execute(
        OPERATIONS["email.search"],
        {
            "keywords": "quarterly report",
            "from": "sender@example.com",
            "unread": True,
            "page_size": 10,
            "page_token": "opaque-page",
        },
        credential(),
    )
    conversation = adapter.execute(
        OPERATIONS["email.conversation.get"], {"conversation_id": "thread-1"}, credential()
    )

    assert searched == {
        "conversations": [
            {
                "conversation_id": "thread-1",
                "subject": "Quarterly report",
                "latest_sender": "Sender <sender@example.com>",
                "latest_date": "Fri, 28 Aug 2026 12:00:00 +0000",
                "snippet": "Hello there",
                "message_count": 1,
                "unread": True,
                "has_attachment": True,
            }
        ],
        "has_more": True,
        "next_page_token": "next",
    }
    assert conversation["messages"][0]["text"] == "Hello there"
    assert conversation["messages"][0]["attachments"] == [
        {"filename": "report.pdf", "mime_type": "application/pdf", "size": 1234}
    ]
    assert "private" not in json.dumps(conversation)
    search_query = parse_qs(urlsplit(calls[0]).query)
    assert search_query["q"] == ['"quarterly report" from:"sender@example.com" is:unread']
    assert search_query["pageToken"] == ["opaque-page"]
    assert "format=full" in calls[-1]


def test_read_new_fetches_every_message_before_removing_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibGmailProviderAdapter()
    bypass_refresh(monkeypatch)
    calls: list[tuple[str, str, bytes | None]] = []
    responses = iter(
        [
            {"messages": [{"id": "message-1"}, {"id": "message-2"}], "nextPageToken": "more"},
            gmail_message("message-1"),
            gmail_message("message-2"),
        ]
    )

    def request_json(url: str, *, method: str, body: bytes | None, **_kwargs):
        calls.append((method, url, body))
        return next(responses)

    def request_bytes(url: str, *, method: str, body: bytes | None, **_kwargs):
        calls.append((method, url, body))
        return b""

    monkeypatch.setattr(adapter, "_request_json", request_json)
    monkeypatch.setattr(adapter, "_request_bytes", request_bytes)
    result = adapter.execute(OPERATIONS["email.read_new"], {}, credential())

    assert result["count"] == 2
    assert result["has_more"] is True
    assert [method for method, _url, _body in calls] == ["GET", "GET", "GET", "POST"]
    assert READ_NEW_QUERY in parse_qs(urlsplit(calls[0][1]).query)["q"]
    assert json.loads(calls[-1][2]) == {"ids": ["message-1", "message-2"], "removeLabelIds": ["UNREAD"]}


def test_read_new_does_not_mark_any_message_when_one_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibGmailProviderAdapter()
    bypass_refresh(monkeypatch)
    modify_called = False
    responses = iter([{"messages": [{"id": "message-1"}, {"id": "message-2"}]}, gmail_message("message-1")])

    def request_json(*_args, **_kwargs):
        try:
            return next(responses)
        except StopIteration:
            raise RuntimeError("second fetch failed") from None

    def request_bytes(*_args, **_kwargs):
        nonlocal modify_called
        modify_called = True
        return b""

    monkeypatch.setattr(adapter, "_request_json", request_json)
    monkeypatch.setattr(adapter, "_request_bytes", request_bytes)
    with pytest.raises(RuntimeError, match="second fetch failed"):
        adapter.execute(OPERATIONS["email.read_new"], {}, credential())
    assert modify_called is False


def test_send_builds_only_bounded_plain_text_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibGmailProviderAdapter()
    bypass_refresh(monkeypatch)
    sent_body: dict[str, str] = {}

    def request_json(_url: str, *, body: bytes | None, **_kwargs):
        assert body is not None
        sent_body.update(json.loads(body))
        return {"id": "sent-message", "threadId": "sent-thread"}

    monkeypatch.setattr(adapter, "_request_json", request_json)
    result = adapter.execute(
        OPERATIONS["email.send"],
        {
            "to": ["to@example.com"],
            "cc": ["cc@example.com"],
            "subject": "Hello",
            "body": "Plain text only",
        },
        credential(),
    )

    raw = base64.urlsafe_b64decode(sent_body["raw"] + "=" * (-len(sent_body["raw"]) % 4))
    message = BytesParser(policy=default).parsebytes(raw)
    assert result == {"sent": True, "message_id": "sent-message", "conversation_id": "sent-thread"}
    assert message.get_content_type() == "text/plain"
    assert message.get_content().rstrip() == "Plain text only"
    assert list(message.iter_attachments()) == []
