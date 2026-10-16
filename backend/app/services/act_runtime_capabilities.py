from __future__ import annotations

from copy import deepcopy

PLAYWRIGHT_CAPABILITY_ID = "browser.playwright"
BROWSER_AUTHENTICATE_CAPABILITY_ID = "browser.authenticate"
TELEGRAM_SEND_FILE_CAPABILITY_ID = "telegram.send_file"
CHAT_SEND_MESSAGE_CAPABILITY_ID = "chat.send_message"

_PLAYWRIGHT_CAPABILITY = {
    "id": PLAYWRIGHT_CAPABILITY_ID,
    "kind": "runtime_capability",
    "title": "Playwright browser automation",
    "description": (
        "Act can use its existing Playwright browser session to navigate and interact with websites. "
        "When a supported login page requires credentials, Act can call browser.authenticate without "
        "receiving the secret. This is a host runtime capability, not an Eidolon function."
    ),
    "risk_level": "medium",
    "requires_invocation_approval": False,
}

_BROWSER_AUTHENTICATE_CAPABILITY = {
    "id": BROWSER_AUTHENTICATE_CAPABILITY_ID,
    "kind": "runtime_capability",
    "category": "agent_private",
    "title": "Browser authenticate",
    "description": (
        "Authenticate the current Playwright page with a configured browser identity when a supported "
        "website requires login. The backend checks the real page origin, fills credentials without "
        "revealing them, preserves the browser session, and pauses for interactive MFA. Treat only "
        "authenticated or already_authenticated as confirmed success; surface other states without claiming success. "
        "For mfa_required, inspect the visible challenge and use chat.send_message to request user action "
        "immediately while keeping the current browser open. "
        "Navigate to the exact user-provided or authenticated service link first; do not guess a tenant, "
        "course, or site instance."
    ),
    "risk_level": "medium",
    "requires_invocation_approval": False,
    "availability": "available",
    "mcp_exposed": True,
    "mcp_read_only": False,
    "mcp_destructive": False,
    "mcp_open_world": True,
    "input_schema": {
        "type": "object",
        "properties": {"identity": {"type": "string", "enum": ["uoft"]}},
        "required": ["identity"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "identity": {"type": "string", "enum": ["uoft"]},
            "status": {
                "type": "string",
                "enum": [
                    "authenticated",
                    "already_authenticated",
                    "mfa_required",
                    "authentication_failed",
                    "user_action_required",
                    "unsupported_origin",
                ],
            },
        },
        "required": ["identity", "status"],
        "additionalProperties": False,
    },
}

_TELEGRAM_SEND_FILE_CAPABILITY = {
    "id": TELEGRAM_SEND_FILE_CAPABILITY_ID,
    "kind": "runtime_capability",
    "category": "agent_private",
    "title": "Send workspace file through Telegram",
    "description": (
        "Send one existing regular file from Act's fixed workspace to the Telegram topic bound to the "
        "current Act session. Use an Act-root-relative path beginning with workspace/, for example "
        "workspace/report.pdf. The backend resolves and confines the path, limits the file to 25 MiB, "
        "uses only the paired act_agent bot, and returns metadata without file contents, chat identifiers, "
        "or credentials. Use only when the user asks to send or deliver the file through Telegram."
    ),
    "risk_level": "medium",
    "requires_invocation_approval": False,
    "availability": "available",
    "mcp_exposed": True,
    "mcp_read_only": False,
    "mcp_destructive": False,
    "mcp_open_world": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            "caption": {"type": "string", "minLength": 1, "maxLength": 1024},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["sent"]},
            "path": {"type": "string"},
            "filename": {"type": "string"},
            "bytes": {"type": "integer", "minimum": 1},
            "message_id": {"type": "integer", "minimum": 1},
        },
        "required": ["status", "path", "filename", "bytes", "message_id"],
        "additionalProperties": False,
    },
}


_CHAT_SEND_MESSAGE_CAPABILITY = {
    "id": CHAT_SEND_MESSAGE_CAPABILITY_ID,
    "kind": "runtime_capability",
    "category": "agent_private",
    "title": "Send current chat message",
    "description": (
        "Immediately send a user-facing message in the current Act conversation without ending the turn. "
        "Telegram turns send through the Act bot to their original paired topic; web turns appear in Chat. "
        "Use for time-sensitive information, requests for user action, or meaningful progress. "
        "Set wait_for_reply=true when the user must respond: the tool waits up to timeout_seconds and "
        "returns their reply within this same turn. For Duo, send the displayed browser number immediately, "
        "ask the user to enter it in Duo Mobile and reply done, and wait for that reply; then inspect the "
        "existing browser page. Do not reload or start a new login after approval. A reply is not proof of "
        "authentication; verify the browser state. notify() is tool output, not user delivery. "
        "A timeout is not approval. Callers cannot choose a session, bot, chat, or topic."
    ),
    "risk_level": "medium",
    "requires_invocation_approval": False,
    "availability": "available",
    "mcp_exposed": True,
    "mcp_read_only": False,
    "mcp_destructive": False,
    "mcp_open_world": False,
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            "wait_for_reply": {"type": "boolean", "default": False},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 120},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["sent"]},
            "message_id": {"type": "integer"},
            "telegram_message_id": {"type": ["integer", "null"]},
            "reply_status": {"type": "string", "enum": ["not_requested", "received", "timed_out", "cancelled"]},
            "reply": {"type": ["string", "null"]},
        },
        "required": ["status", "message_id", "telegram_message_id", "reply_status", "reply"],
        "additionalProperties": False,
    },
}


def act_private_tool_entries() -> list[dict]:
    return [
        deepcopy(_BROWSER_AUTHENTICATE_CAPABILITY),
        deepcopy(_TELEGRAM_SEND_FILE_CAPABILITY),
        deepcopy(_CHAT_SEND_MESSAGE_CAPABILITY),
    ]


def act_runtime_capability_catalog() -> list[dict]:
    fields = (
        "id",
        "kind",
        "title",
        "description",
        "risk_level",
        "requires_invocation_approval",
    )
    return [
        {field: entry[field] for field in fields}
        for entry in (
            _PLAYWRIGHT_CAPABILITY,
            _BROWSER_AUTHENTICATE_CAPABILITY,
            _TELEGRAM_SEND_FILE_CAPABILITY,
            _CHAT_SEND_MESSAGE_CAPABILITY,
        )
    ]
