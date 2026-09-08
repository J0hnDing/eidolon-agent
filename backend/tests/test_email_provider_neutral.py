from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.execution.context import InvocationContext
from app.integrations.authorization import _issue_authorized_integration_invocation
from app.integrations.invocation import IntegrationInvocationError, IntegrationInvocationService
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.integrations.runtime import IntegrationRuntimeError, ProviderExecutionResult
from app.services.email_contracts import decode_composite_cursor
from app.services.outlook_provider import (
    OUTLOOK_CLIENT_SECRET_NAMESPACE,
    OUTLOOK_OAUTH_REDIRECT_URI,
    OUTLOOK_SCOPE,
    OUTLOOK_SECRET_NAMESPACE,
    OutlookProviderAdapter,
    _safe_graph_cursor,
    outlook_authorization_url,
)
from app.services.secret_store import WindowsCredentialSecretStore


def _summary(provider: str, conversation_id: str, timestamp: str) -> dict[str, object]:
    return {
        "provider": provider,
        "conversation_id": conversation_id,
        "subject": conversation_id,
        "latest_sender": f"{provider}@example.com",
        "latest_timestamp": timestamp,
        "snippet": "snippet",
        "message_count": 1,
        "unread": True,
        "has_attachment": False,
    }


def test_windows_secret_store_has_outlook_namespaces() -> None:
    store = object.__new__(WindowsCredentialSecretStore)
    reference = "a" * 32
    assert store._target(reference, OUTLOOK_CLIENT_SECRET_NAMESPACE) == "Eidolon/MicrosoftOAuth/" + reference
    assert store._target(reference, OUTLOOK_SECRET_NAMESPACE) == "Eidolon/Outlook/" + reference


class _ProviderPolicy:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.failures = failures or set()

    def authorize(self, context, operation, input_json, *, provider_id, approval=None):  # noqa: ANN001, ANN201
        del approval
        self.calls.append(provider_id)
        if provider_id in self.failures:
            raise RuntimeError(f"{provider_id} is unavailable")
        return _issue_authorized_integration_invocation(
            context=context,
            operation=operation,
            input_json=input_json,
            provider_id=provider_id,
            connection_id=10 if provider_id == "gmail" else 20,
            provider_account_id=f"{provider_id}-account",
            resource=None,
            authorization_id=None,
        )


class _ProviderRuntime:
    def __init__(self, outputs: dict[str, dict[str, object]], failures: set[str] | None = None) -> None:
        self.outputs = outputs
        self.failures = failures or set()
        self.calls: list[str] = []

    def execute(self, invocation):  # noqa: ANN001, ANN201
        self.calls.append(invocation.provider_id)
        if invocation.provider_id in self.failures:
            raise IntegrationRuntimeError("provider_timeout", "provider timed out", retry_after=9)
        return ProviderExecutionResult(output=self.outputs[invocation.provider_id])


def _invocation_service(policy: _ProviderPolicy, runtime: _ProviderRuntime) -> IntegrationInvocationService:
    compatibility = SimpleNamespace(
        secret_store=None,
        provider_connected=lambda _provider: True,
        operation_available=lambda _operation: True,
        _connection=lambda _provider: None,
    )
    service = IntegrationInvocationService(
        SimpleNamespace(),
        compatibility_service=compatibility,
        runtime=runtime,
    )
    service.policy = policy
    return service


def test_email_registry_is_shared_and_requires_provider_selectors() -> None:
    expected = {
        "email.search",
        "email.conversation.get",
        "email.read_new",
        "email.read_and_mark_new",
        "email.send",
    }
    assert {operation.id for operation in DEFAULT_INTEGRATION_REGISTRY.list() if operation.id.startswith("email.")} == expected
    for operation_id in expected:
        assert DEFAULT_INTEGRATION_REGISTRY.operation_provider_set(operation_id) == ("gmail", "outlook")
        assert "gmail." not in DEFAULT_INTEGRATION_REGISTRY.get(operation_id).resource.type
    assert DEFAULT_INTEGRATION_REGISTRY.get("email.search").input_schema["required"] == ["providers"]
    assert DEFAULT_INTEGRATION_REGISTRY.get("email.send").input_schema["required"][0] == "provider"


def test_search_fans_out_with_per_provider_size_and_global_order() -> None:
    policy = _ProviderPolicy()
    runtime = _ProviderRuntime(
        {
            "gmail": {
                "conversations": [_summary("gmail", "g1", "2026-08-28T12:00:00Z")],
                "has_more": True,
                "next_page_token": "gmail-cursor",
            },
            "outlook": {
                "conversations": [_summary("outlook", "o1", "2026-08-28T13:00:00Z")],
                "has_more": True,
                "next_page_token": "outlook-cursor",
            },
        }
    )
    result = _invocation_service(policy, runtime).execute(
        InvocationContext(principal_kind="user", origin="http"),
        "email.search",
        {"providers": ["gmail", "outlook"], "keywords": "report", "page_size": 7},
    )

    assert [item["conversation_id"] for item in result.output["conversations"]] == ["o1", "g1"]
    assert result.output["provider_errors"] == []
    cursors = decode_composite_cursor(result.output["next_page_token"], ("gmail", "outlook"))
    assert cursors == {"gmail": "gmail-cursor", "outlook": "outlook-cursor"}
    assert policy.calls == ["gmail", "outlook"]
    assert runtime.calls == ["gmail", "outlook"]


def test_search_partial_failure_returns_provider_error_and_malformed_cursor_is_whole_call_error() -> None:
    policy = _ProviderPolicy()
    runtime = _ProviderRuntime(
        {"gmail": {"conversations": [], "has_more": False, "next_page_token": None}},
        failures={"outlook"},
    )
    result = _invocation_service(policy, runtime).execute(
        InvocationContext(principal_kind="user", origin="http"),
        "email.search",
        {"providers": ["gmail", "outlook"], "keywords": "report"},
    )
    assert result.output["conversations"] == []
    assert result.output["provider_errors"] == [
        {
            "provider": "outlook",
            "error_type": "provider_timeout",
            "message": "provider timed out",
            "retry_after_seconds": 9,
        }
    ]

    with pytest.raises(IntegrationInvocationError) as exc_info:
        _invocation_service(policy, runtime).execute(
            InvocationContext(principal_kind="user", origin="http"),
            "email.search",
            {"providers": ["gmail", "outlook"], "keywords": "report", "page_token": "not-a-token"},
        )
    assert exc_info.value.error_type == "invalid_input"


def test_read_results_sort_newest_first_and_mark_outcomes_remain_provider_scoped() -> None:
    policy = _ProviderPolicy()
    runtime = _ProviderRuntime(
        {
            "gmail": {
                "messages": [
                    {
                        "provider": "gmail",
                        "message_id": "g1",
                        "conversation_id": "t1",
                        "from": "a@example.com",
                        "to": [],
                        "cc": [],
                        "bcc": [],
                        "subject": "old",
                        "timestamp": "2026-08-28T12:00:00Z",
                        "snippet": "",
                        "text": "",
                        "unread": True,
                        "attachments": [],
                    }
                ],
                "has_more": False,
                "mark_outcomes": [{
                    "provider": "gmail",
                    "marked_message_ids": ["g1"],
                    "marked_count": 1,
                    "failed_message_ids": [],
                    "failed_count": 0,
                    "unknown_message_ids": [],
                    "unknown_count": 0,
                }],
                "provider_errors": [],
            },
            "outlook": {
                "messages": [
                    {
                        "provider": "outlook",
                        "message_id": "o1",
                        "conversation_id": "t2",
                        "from": "b@example.com",
                        "to": [],
                        "cc": [],
                        "bcc": [],
                        "subject": "new",
                        "timestamp": "2026-08-28T13:00:00Z",
                        "snippet": "",
                        "text": "",
                        "unread": True,
                        "attachments": [],
                    }
                ],
                "has_more": False,
                "mark_outcomes": [{
                    "provider": "outlook",
                    "marked_message_ids": [],
                    "marked_count": 0,
                    "failed_message_ids": [],
                    "failed_count": 0,
                    "unknown_message_ids": ["o1"],
                    "unknown_count": 1,
                }],
                "provider_errors": [{
                    "provider": "outlook",
                    "error_type": "partial_mutation",
                    "message": "Outlook read state was only partially reconciled",
                }],
            },
        }
    )
    result = _invocation_service(policy, runtime).execute(
        InvocationContext(principal_kind="user", origin="http"),
        "email.read_and_mark_new",
        {"providers": ["gmail", "outlook"]},
    )
    assert [message["message_id"] for message in result.output["messages"]] == ["o1", "g1"]
    assert result.output["count"] == 2
    assert {item["provider"] for item in result.output["mark_outcomes"]} == {"gmail", "outlook"}
    assert result.output["provider_errors"][0]["error_type"] == "partial_mutation"


def test_outlook_oauth_and_graph_cursor_safety() -> None:
    url = outlook_authorization_url("client-id", "state-value", "challenge-value")
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == [OUTLOOK_OAUTH_REDIRECT_URI]
    assert query["code_challenge_method"] == ["S256"]
    assert query["prompt"] == ["select_account"]
    assert query["scope"] == [" ".join(OUTLOOK_SCOPE)]
    assert _safe_graph_cursor("opaque-skip-token") == "opaque-skip-token"
    with pytest.raises(Exception):
        _safe_graph_cursor("https://evil.example/steal")


class _GraphTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = iter(responses)
        self.requests: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def request_json(self, url, *, method, headers, body, timeout, max_bytes, oauth_request=False):  # noqa: ANN001, ANN201
        del timeout, max_bytes, oauth_request
        self.requests.append((url, method, headers, body))
        return next(self.responses)

    def request_bytes(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise AssertionError("Graph fake should use JSON responses")


def test_outlook_read_and_mark_reconciles_ambiguous_batch_subresponses() -> None:
    transport = _GraphTransport(
        [
            {"access_token": "access-token"},
            {
                "value": [
                    {
                        "id": "message-1",
                        "conversationId": "thread-1",
                        "subject": "Subject",
                        "from": {"emailAddress": {"address": "sender@example.com"}},
                        "receivedDateTime": "2026-08-28T13:00:00Z",
                        "body": {"content": "Plain text"},
                        "bodyPreview": "Plain text",
                        "isRead": False,
                        "hasAttachments": False,
                    }
                ]
            },
            {"responses": [{"id": "1", "status": 500}]},
            {"id": "message-1", "isRead": True},
        ]
    )
    adapter = OutlookProviderAdapter(transport)
    result = adapter.execute(
        DEFAULT_INTEGRATION_REGISTRY.get("email.read_and_mark_new"),
        {},
        json.dumps({"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"}),
    )
    outcome = result["mark_outcomes"][0]
    assert outcome["marked_message_ids"] == ["message-1"]
    assert outcome["unknown_message_ids"] == []
    assert result["provider_errors"] == []
    assert transport.requests[2][0].endswith("/$batch")
    assert transport.requests[3][1] == "GET"
    assert transport.requests[2][2]["Prefer"] == 'IdType="ImmutableId", outlook.body-content-type="text"'
    batch_request = json.loads(transport.requests[2][3].decode("utf-8"))["requests"][0]
    assert batch_request["headers"]["Prefer"] == 'IdType="ImmutableId"'


def test_outlook_search_converts_filters_and_send_uses_an_immutable_plain_text_draft() -> None:
    search_transport = _GraphTransport(
        [
            {"access_token": "access-token"},
            {
                "value": [],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=next-token",
            },
        ]
    )
    adapter = OutlookProviderAdapter(search_transport)
    search_result = adapter.execute(
        DEFAULT_INTEGRATION_REGISTRY.get("email.search"),
        {
            "keywords": 'quarterly "budget"',
            "from": "sender@example.com",
            "subject": "Q1",
            "after": "2026-01-01",
            "before": "2026-02-01",
            "unread": True,
            "has_attachment": False,
            "page_size": 7,
        },
        json.dumps({"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"}),
    )
    query = parse_qs(urlsplit(search_transport.requests[1][0]).query)
    assert query["$top"] == ["7"]
    assert query["$filter"] == [
        "receivedDateTime ge 2026-01-01T00:00:00Z and receivedDateTime lt 2026-02-02T00:00:00Z and isRead eq false and hasAttachments eq false"
    ]
    assert query["$search"] == ['"quarterly \\"budget\\" AND from:sender@example.com AND subject:Q1"']
    assert search_result["next_page_token"] == "next-token"

    send_transport = _GraphTransport(
        [
            {"access_token": "access-token"},
            {"id": "immutable-message", "conversationId": "thread-1"},
            {},
        ]
    )
    send_result = OutlookProviderAdapter(send_transport).execute(
        DEFAULT_INTEGRATION_REGISTRY.get("email.send"),
        {
            "to": ["recipient@example.com"],
            "subject": "Hello",
            "body": "Plain text body",
        },
        json.dumps({"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"}),
    )
    assert send_result == {
        "provider": "outlook",
        "sent": True,
        "message_id": "immutable-message",
        "conversation_id": "thread-1",
    }
    assert send_transport.requests[1][2]["Prefer"] == 'IdType="ImmutableId", outlook.body-content-type="text"'
    assert send_transport.requests[2][0].endswith("/me/messages/immutable-message/send")
    assert send_transport.requests[2][2]["Prefer"] == 'IdType="ImmutableId", outlook.body-content-type="text"'


def test_outlook_read_new_is_inbox_focused_unread_and_recent() -> None:
    transport = _GraphTransport(
        [
            {"access_token": "access-token"},
            {"value": []},
        ]
    )
    OutlookProviderAdapter(transport).execute(
        DEFAULT_INTEGRATION_REGISTRY.get("email.read_new"),
        {},
        json.dumps({"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"}),
    )
    request = transport.requests[1]
    parsed = urlsplit(request[0])
    query = parse_qs(parsed.query)
    assert parsed.path == "/v1.0/me/mailFolders/inbox/messages"
    assert "isRead eq false" in query["$filter"][0]
    assert "inferenceClassification eq 'focused'" in query["$filter"][0]
    assert "receivedDateTime ge " in query["$filter"][0]
    assert query["$expand"] == ["attachments($select=name,contentType,size)"]


def test_legacy_connection_migration_preserves_rows_and_foreign_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE integration_connections ("
            "id INTEGER PRIMARY KEY, provider VARCHAR(32) NOT NULL, "
            "secret_store_id VARCHAR(64) NOT NULL, secret_reference VARCHAR(256) NOT NULL, "
            "status VARCHAR(32) NOT NULL, account_login VARCHAR(128) NOT NULL, "
            "account_id VARCHAR(128) NOT NULL, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, last_validated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_integration_connections_provider ON integration_connections(provider)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE integration_grants ("
            "id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL "
            "REFERENCES integration_connections(id) ON DELETE CASCADE, value TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO integration_connections "
            "(id, provider, secret_store_id, secret_reference, status, account_login, account_id, created_at, updated_at, last_validated_at) "
            "VALUES (1, 'gmail', 'windows', 'gmail-secret', 'connected', 'one@example.com', 'account-one', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "(2, 'outlook', 'windows', 'outlook-secret', 'connected', 'two@example.com', 'account-two', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO integration_grants (id, connection_id, value) VALUES (10, 1, 'grant-one'), (20, 2, 'grant-two')"
        )

    monkeypatch.setattr(db_module, "engine", legacy_engine)
    db_module._migrate_integration_connection_uniqueness(
        inspect(legacy_engine),
        {"integration_connections"},
    )

    with legacy_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, provider, is_default, secret_reference FROM integration_connections ORDER BY id")
        ).all()
        assert rows == [
            (1, "gmail", 1, "gmail-secret"),
            (2, "outlook", 1, "outlook-secret"),
        ]
        assert connection.execute(text("SELECT COUNT(*) FROM integration_grants")).scalar_one() == 2
        connection.execute(
            text(
                "INSERT INTO integration_connections "
                "(id, provider, is_default, secret_store_id, secret_reference, credential_kind, status, account_login, account_id, created_at, updated_at, last_validated_at) "
                "VALUES (3, 'gmail', 0, 'windows', 'gmail-secret-2', 'token', 'connected', 'three@example.com', 'account-three', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.commit()

    with legacy_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO integration_connections "
                    "(id, provider, is_default, secret_store_id, secret_reference, credential_kind, status, account_login, account_id, created_at, updated_at, last_validated_at) "
                    "VALUES (4, 'gmail', 1, 'windows', 'gmail-secret-4', 'token', 'connected', 'four@example.com', 'account-four', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
        transaction.rollback()
