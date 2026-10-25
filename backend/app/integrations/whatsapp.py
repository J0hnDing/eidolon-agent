from __future__ import annotations

from app.integrations.types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    OperationPresentation,
    ProviderSpec,
    ResourceSpec,
    RiskLevel,
)

_CHAT_JID = {
    "type": "string",
    "minLength": 3,
    "maxLength": 128,
    "pattern": r"^(?:[0-9]{6,20}@s\.whatsapp\.net|[0-9]{6,20}@lid|[0-9]{6,20}(?:-[0-9]{6,20})?@g\.us)$",
}
_RECIPIENT = {
    "type": "string",
    "minLength": 3,
    "maxLength": 128,
    "pattern": r"^(?:\+[1-9][0-9]{6,14}|[0-9]{6,20}@s\.whatsapp\.net|[0-9]{6,20}@lid|[0-9]{6,20}(?:-[0-9]{6,20})?@g\.us)$",
}
_MESSAGE_ID = {"type": "string", "minLength": 1, "maxLength": 256}
_PAGE_SIZE = {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}
_CURSOR = {"type": "string", "minLength": 1, "maxLength": 512}
_COVERAGE = {
    "type": "object",
    "properties": {
        "retention_cutoff": {"type": "string", "format": "date-time"},
        "oldest_message_at": {"type": ["string", "null"], "format": "date-time"},
        "last_synced_at": {"type": ["string", "null"], "format": "date-time"},
        "sync_status": {"type": "string", "minLength": 1, "maxLength": 32},
    },
    "required": ["retention_cutoff", "oldest_message_at", "last_synced_at", "sync_status"],
    "additionalProperties": False,
}


def _object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _page(item_schema: dict) -> dict:
    return _object(
        {
            "items": {"type": "array", "maxItems": 100, "items": item_schema},
            "next_cursor": {"type": ["string", "null"], "maxLength": 512},
            "coverage": _COVERAGE,
        },
        ["items", "next_cursor", "coverage"],
    )


_CONTACT = _object(
    {
        "id": _CHAT_JID,
        "name": {"type": "string", "maxLength": 256},
        "phone": {"type": ["string", "null"], "maxLength": 20},
    },
    ["id", "name", "phone"],
)
_CHAT = _object(
    {
        "id": _CHAT_JID,
        "name": {"type": "string", "maxLength": 256},
        "type": {"type": "string", "enum": ["direct", "group"]},
        "last_message_at": {"type": ["string", "null"], "format": "date-time"},
    },
    ["id", "name", "type", "last_message_at"],
)
_MESSAGE = _object(
    {
        "id": _MESSAGE_ID,
        "chat_id": _CHAT_JID,
        "sender_id": _CHAT_JID,
        "from_me": {"type": "boolean"},
        "timestamp": {"type": "string", "format": "date-time"},
        "text": {"type": ["string", "null"], "maxLength": 20000},
        "type": {"type": "string", "minLength": 1, "maxLength": 80},
        "reply_to_message_id": {"type": ["string", "null"], "maxLength": 256},
    },
    [
        "id",
        "chat_id",
        "sender_id",
        "from_me",
        "timestamp",
        "text",
        "type",
        "reply_to_message_id",
    ],
)

_ERRORS = (
    "connection_unavailable",
    "invalid_input",
    "invalid_recipient",
    "not_found",
    "provider_forbidden",
    "provider_timeout",
    "provider_unavailable",
    "rate_limited",
    "response_too_large",
    "operation_undeclared",
    "authorization_missing_or_stale",
    "internal_failure",
)


def _presentation(operation: str, example: dict) -> OperationPresentation:
    return OperationPresentation(
        usage_example={"operation": operation, "input": example},
        normalized_errors=_ERRORS,
        open_world=False,
    )


WHATSAPP_OPERATIONS = (
    IntegrationOperationSpec(
        id="whatsapp.contact.search",
        title="Search WhatsApp contacts",
        description="Search contacts available in Eidolon's recent WhatsApp cache.",
        input_schema=_object(
            {"query": {"type": "string", "minLength": 1, "maxLength": 200}, "page_size": _PAGE_SIZE, "cursor": _CURSOR},
            [],
        ),
        output_schema=_page(_CONTACT),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("whatsapp.contact"),
        risk=RiskLevel.LOW,
        presentation=_presentation("whatsapp.contact.search", {"query": "Jordan", "page_size": 50}),
    ),
    IntegrationOperationSpec(
        id="whatsapp.chat.list",
        title="List WhatsApp chats",
        description="List direct and group chats from the available 30-day WhatsApp cache.",
        input_schema=_object(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "type": {"type": "string", "enum": ["direct", "group"]},
                "page_size": _PAGE_SIZE,
                "cursor": _CURSOR,
            },
            [],
        ),
        output_schema=_page(_CHAT),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("whatsapp.chat"),
        risk=RiskLevel.LOW,
        presentation=_presentation("whatsapp.chat.list", {"page_size": 50}),
    ),
    IntegrationOperationSpec(
        id="whatsapp.message.list",
        title="Read WhatsApp chat messages",
        description="Read recent cached text and message metadata for one WhatsApp chat without marking it read.",
        input_schema=_object(
            {"chat_id": _CHAT_JID, "page_size": _PAGE_SIZE, "cursor": _CURSOR},
            ["chat_id"],
        ),
        output_schema=_page(_MESSAGE),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("whatsapp.chat", ("chat_id",)),
        risk=RiskLevel.LOW,
        presentation=_presentation(
            "whatsapp.message.list", {"chat_id": "12025550123@s.whatsapp.net", "page_size": 50}
        ),
    ),
    IntegrationOperationSpec(
        id="whatsapp.message.search",
        title="Search WhatsApp messages",
        description="Search recent cached text messages, optionally within one chat and date range.",
        input_schema=_object(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "chat_id": _CHAT_JID,
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "page_size": _PAGE_SIZE,
                "cursor": _CURSOR,
            },
            ["query"],
        ),
        output_schema=_page(_MESSAGE),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("whatsapp.message"),
        risk=RiskLevel.LOW,
        presentation=_presentation("whatsapp.message.search", {"query": "project update", "page_size": 50}),
    ),
    IntegrationOperationSpec(
        id="whatsapp.message.send",
        title="Send WhatsApp text message",
        description="Send one text message to the exact WhatsApp recipient after the user approves the complete action.",
        input_schema=_object(
            {
                "recipient": _RECIPIENT,
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                "reply_to_message_id": _MESSAGE_ID,
            },
            ["recipient", "text"],
        ),
        output_schema=_object(
            {
                "accepted": {"type": "boolean", "const": True},
                "message_id": _MESSAGE_ID,
                "chat_id": _CHAT_JID,
            },
            ["accepted", "message_id", "chat_id"],
        ),
        effects=frozenset({IntegrationEffect.SEND}),
        resource=ResourceSpec("whatsapp.message", ("recipient",)),
        risk=RiskLevel.HIGH,
        presentation=_presentation(
            "whatsapp.message.send",
            {"recipient": "+12025550123", "text": "I will be there at 3."},
        ),
    ),
)


WHATSAPP_PROVIDER = ProviderSpec(
    id="whatsapp",
    display_name="WhatsApp",
    supported_operations=frozenset(operation.id for operation in WHATSAPP_OPERATIONS),
)
