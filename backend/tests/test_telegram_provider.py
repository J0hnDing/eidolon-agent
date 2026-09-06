from __future__ import annotations

import html

import pytest

from app.services.invocation_approval_presentation import build_approval_presentation
from app.services.telegram_provider import (
    FakeTelegramBotApi,
    InMemoryTelegramOffsetStore,
    PairingCodeStore,
    TelegramLongPollWorker,
    TelegramProviderError,
    build_callback_data,
    build_proposal_callback_data,
    create_pairing_code,
    edit_agent_proposal_outcome,
    edit_approval_outcome,
    hash_callback_nonce,
    parse_callback_data,
    parse_callback_update,
    parse_pairing_update,
    parse_start_command,
    send_agent_proposal_request,
    send_approval_request,
    send_notification,
    validate_callback_origin,
    validate_pairing_message,
)


def _pairing_update(code: str, *, chat_id: int = 42, user_id: int = 7, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id},
            "text": f"/start {code}",
        },
    }


def _callback_update(data: str, *, chat_id: int = 42, user_id: int = 7, update_id: int = 2) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": 10, "chat": {"id": chat_id, "type": "private"}},
        },
    }


def test_notification_is_bounded_escaped_and_supports_http_link() -> None:
    api = FakeTelegramBotApi()
    result = send_notification(api, 42, title="<Title>", description="A & B", link="http://example.test/item")

    assert result == {"sent": True, "message_id": 1}
    message = api.sent_messages[0]
    assert message["text"] == "🔔 <b>&lt;Title&gt;</b>\nA &amp; B"
    assert "&lt;Title&gt;" in message["text"]
    assert "A &amp; B" in message["text"]
    assert message["reply_markup"]["inline_keyboard"][0][0]["url"] == "http://example.test/item"
    with pytest.raises(TelegramProviderError):
        send_notification(api, 42, title="x" * 121, description="ok")
    with pytest.raises(TelegramProviderError):
        send_notification(api, 42, title="ok", description="ok", link="javascript:alert(1)")


def test_notification_alert_uses_red_exclamation_marker() -> None:
    api = FakeTelegramBotApi()

    send_notification(api, 42, title="Weekly report failed", description="rate_limited: Try later", alert=True)

    assert api.sent_messages[0]["text"] == "❗ <b>Weekly report failed</b>\nrate_limited: Try later"
    with pytest.raises(TelegramProviderError, match="alert flag"):
        send_notification(api, 42, title="Invalid", description="Invalid", alert="yes")


def test_pairing_parser_requires_private_start_and_code_is_one_time() -> None:
    pairing = create_pairing_code(now=100.0)
    assert parse_start_command(f"/start@eidolon_bot {pairing.code}") == pairing.code
    assert parse_start_command("/start wrong extra") is None
    assert parse_pairing_update({**_pairing_update(pairing.code), "message": {"chat": {"id": 42, "type": "group"}}}) is None

    store = PairingCodeStore()
    pending = store.create(now=100.0)
    with pytest.raises(TelegramProviderError):
        store.consume(_pairing_update(pending.code, user_id=99), expected_user_id=7, now=100.0)
    accepted = store.consume(_pairing_update(pending.code), expected_user_id=7, now=100.0)
    assert accepted.chat_id == 42
    with pytest.raises(TelegramProviderError):
        store.consume(_pairing_update(pending.code), expected_user_id=7, now=100.0)

    with pytest.raises(TelegramProviderError):
        validate_pairing_message(_pairing_update(pairing.code), "0" * 64, now=700.0, expires_at=200.0)


def test_approval_delivery_chunks_and_puts_buttons_on_status_message() -> None:
    api = FakeTelegramBotApi()
    presentation = build_approval_presentation(
        approval_id=123,
        action="email.send",
        caller="assistant",
        reason="The user requested this notification.",
        input_json={"to": ["person@example.com"], "body": "<&" * 9000},
    )
    delivery = send_approval_request(api, 42, presentation, nonce="fixed_nonce_123")

    assert len(api.sent_messages) > 1
    assert all(len(message["text"]) <= 4096 for message in api.sent_messages)
    assert api.sent_messages[0]["reply_markup"] is not None
    assert all(message["reply_markup"] is None for message in api.sent_messages[1:])
    callback_values = [
        button["callback_data"]
        for button in api.sent_messages[0]["reply_markup"]["inline_keyboard"][0]
    ]
    assert callback_values == [delivery.approve_callback_data, delivery.deny_callback_data]
    assert all(len(value.encode("utf-8")) <= 64 for value in callback_values)
    assert hash_callback_nonce("fixed_nonce_123") == delivery.nonce_hash
    assert "&lt;&amp;" in "".join(message["text"] for message in api.sent_messages)


def test_email_approval_uses_backend_preset_and_outcome_replaces_controls() -> None:
    api = FakeTelegramBotApi()
    presentation = build_approval_presentation(
        approval_id=7,
        action="email.send",
        caller="mcp",
        input_json={
            "to": ["dingjh0602@gmail.com"],
            "subject": "Eidolon email integration test",
            "body": "This is a test email sent through Eidolon.",
        },
        reason="The user requested a self-addressed test email.",
    )

    send_approval_request(api, 42, presentation, nonce="fixed_nonce_123")

    initial = api.sent_messages[0]["text"]
    assert initial.startswith("🤔 <b>Approval required</b>")
    assert "<b>Action:</b> <code>email.send</code>" in initial
    assert "<b>Caller:</b> <code>mcp</code>" in initial
    assert "<b>To:</b> dingjh0602@gmail.com" in initial
    assert "<b>Subject:</b> Eidolon email integration test" in initial
    assert "<b>Body:</b>\nThis is a test email sent through Eidolon." in initial
    assert "<b>Why:</b>\nThe user requested a self-addressed test email." in initial
    assert [button["text"] for button in api.sent_messages[0]["reply_markup"]["inline_keyboard"][0]] == [
        "✅ Approve",
        "❌ Deny",
    ]

    edit_approval_outcome(
        api,
        42,
        1,
        presentation,
        decision_status="approved",
        execution_status="succeeded",
    )

    edited = api.edited_messages[0]
    assert edited["text"].startswith("✅ <b>Approved · Executed</b>")
    assert "Sent email" in edited["text"]
    assert "<b>To:</b> dingjh0602@gmail.com" in edited["text"]
    assert "<b>Subject:</b> Eidolon email integration test" in edited["text"]
    assert edited["reply_markup"] == {"inline_keyboard": []}


def test_generic_approval_humanizes_nested_inputs_without_json() -> None:
    api = FakeTelegramBotApi()
    presentation = build_approval_presentation(
        approval_id=8,
        action="example.run",
        caller="web_app",
        input_json={
            "recipient_name": "Alice",
            "options": {"send_now": True, "tags": ["one", "two"]},
        },
        reason="The caller needs to complete the requested action.",
    )

    send_approval_request(api, 42, presentation, nonce="fixed_nonce_123")

    rendered = html.unescape("\n".join(message["text"] for message in api.sent_messages))
    assert "<b>Recipient name:</b> Alice" in rendered
    assert "Send now: Yes" in rendered
    assert "Tags: one, two" in rendered
    assert '"recipient_name"' not in rendered
    assert "{" not in rendered


def test_failed_email_outcome_includes_sanitized_code_meaning_and_metadata() -> None:
    api = FakeTelegramBotApi()
    presentation = build_approval_presentation(
        approval_id=9,
        action="email.send",
        caller="mcp",
        input_json={
            "to": ["alice@example.com"],
            "subject": "Meeting notes",
            "body": "Attached are the notes.",
        },
        reason="The user requested the email.",
    )

    edit_approval_outcome(
        api,
        42,
        3,
        presentation,
        decision_status="approved",
        execution_status="failed",
        error_type="rate_limited",
        error_message="The provider asked Eidolon to retry later.",
    )

    edited = api.edited_messages[0]["text"]
    assert edited.startswith("⚠️ <b>Approved · Execution failed</b>")
    assert "<b>To:</b> alice@example.com" in edited
    assert "<b>Subject:</b> Meeting notes" in edited
    assert "<b>Error code:</b> rate_limited" in edited
    assert "rate limit was reached" in edited
    assert "The provider asked Eidolon to retry later." in edited


def test_callback_data_and_origin_validation_are_bounded() -> None:
    data, digest = build_callback_data(12, "approve", "nonce_1234")
    assert parse_callback_data(data) == ("approve", 12, "nonce_1234")
    callback = parse_callback_update(_callback_update(data))
    assert callback is not None
    validate_callback_origin(callback, expected_chat_id=42, expected_user_id=7, expected_nonce_hash=digest)
    with pytest.raises(TelegramProviderError):
        validate_callback_origin(callback, expected_chat_id=99, expected_user_id=7, expected_nonce_hash=digest)
    with pytest.raises(TelegramProviderError):
        build_callback_data(12, "approve", "x" * 64)


def test_agent_proposal_delivery_has_namespaced_callbacks_and_complete_instruction() -> None:
    api = FakeTelegramBotApi()

    delivery = send_agent_proposal_request(
        api,
        42,
        proposal_id=4,
        title="Finish the report",
        rationale="The report is the next actionable todo.",
        instruction="Open the report workspace and complete the remaining analysis.",
        actions="Inspect sources, update the draft, and run its checks.",
        references=["todo: 12", "goal: publish report"],
        nonce="fixed_nonce_123",
    )

    assert delivery.approve_callback_data.startswith("proposal:approve:4:")
    callback = parse_callback_update(_callback_update(delivery.approve_callback_data))
    assert callback is not None
    assert callback.kind == "proposal"
    assert callback.approval_id == 4
    validate_callback_origin(
        callback,
        expected_chat_id=42,
        expected_user_id=7,
        expected_nonce_hash=delivery.nonce_hash,
    )
    rendered = html.unescape("\n".join(message["text"] for message in api.sent_messages))
    assert "Finish the report" in rendered
    assert "Open the report workspace and complete the remaining analysis." in rendered
    assert "todo: 12" in rendered

    edit_agent_proposal_outcome(
        api,
        42,
        delivery.message_ids[0],
        title="Finish the report",
        rationale="The report is the next actionable todo.",
        instruction="Open the report workspace and complete the remaining analysis.",
        actions="Inspect sources, update the draft, and run its checks.",
        references=["todo: 12"],
        status="approved",
        execution_status="queued",
    )

    assert api.edited_messages[-1]["text"].startswith("✅ <b>Approved · Act queued</b>")
    assert api.edited_messages[-1]["reply_markup"] == {"inline_keyboard": []}

    callback_data, _ = build_proposal_callback_data(4, "deny", "fixed_nonce_123")
    assert len(callback_data.encode("utf-8")) <= 64


def test_worker_rejects_webhook_and_wrong_origin_but_persists_updates() -> None:
    api = FakeTelegramBotApi(webhook_url="https://example.test/hook")
    worker = TelegramLongPollWorker(api)
    with pytest.raises(TelegramProviderError) as exc_info:
        worker.ensure_long_polling_ready()
    assert exc_info.value.error_type == "webhook_conflict"

    api.webhook_url = ""
    callback_data, _ = build_callback_data(8, "approve", "nonce_1234")
    api.updates = [_callback_update(callback_data, chat_id=99), _callback_update(callback_data, update_id=3)]
    offset = InMemoryTelegramOffsetStore()
    received = []
    worker = TelegramLongPollWorker(
        api,
        offset_store=offset,
        expected_chat_id=42,
        expected_user_id=7,
        on_callback=received.append,
    )
    assert worker.poll_once() == 2
    assert offset.get_offset() == 4
    assert len(received) == 1
    assert [item["show_alert"] for item in api.answered_callbacks] == [True, False]


def test_worker_processes_pairing_and_callbacks_only() -> None:
    api = FakeTelegramBotApi()
    pairing_code = create_pairing_code().code
    callback_data, _ = build_callback_data(1, "deny", "nonce_1234")
    api.updates = [
        _pairing_update(pairing_code, update_id=5),
        {"update_id": 6, "message": {"chat": {"id": 42, "type": "private"}, "text": "hello"}},
        _callback_update(callback_data, update_id=7),
    ]
    pairings = []
    callbacks = []
    worker = TelegramLongPollWorker(api, on_pairing=pairings.append, on_callback=callbacks.append)
    assert worker.poll_once() == 3
    assert [item.code for item in pairings] == [pairing_code]
    assert [item.decision for item in callbacks] == ["deny"]
