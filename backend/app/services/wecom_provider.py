"""Small, fixed-surface client helpers for the WeCom Intelligent Bot socket."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.services.github_provider import IntegrationProviderError

WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
WECOM_AGENT_ID = "observer"
WECOM_SECRET_NAMESPACE = "wecom"
WECOM_PAIRING_TTL_SECONDS = 600
WECOM_HEARTBEAT_SECONDS = 30.0
WECOM_HEARTBEAT_TIMEOUT_SECONDS = 60.0
WECOM_SUBSCRIBE_TIMEOUT_SECONDS = 15.0
WECOM_MAX_STREAM_BYTES = 20_480
WECOM_MAX_FRAME_BYTES = 2_000_000
WECOM_CALLBACK_COMMAND = "aibot_msg_callback"
WECOM_EVENT_COMMAND = "aibot_event_callback"
WECOM_SUBSCRIBE_COMMAND = "aibot_subscribe"
WECOM_PING_COMMAND = "ping"
WECOM_RESPONSE_COMMAND = "aibot_respond_msg"
WECOM_SEND_COMMAND = "aibot_send_msg"


class WeComProviderError(IntegrationProviderError):
    """A normalized WeCom protocol or transport failure."""


class WeComSocket(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ParsedWeComMessage:
    message_id: str
    request_id: str
    user_id: str
    chat_type: str
    text: str


def validate_bot_id(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128 or any(character.isspace() for character in value):
        raise WeComProviderError("invalid_input", "WeCom Bot ID is invalid")
    return value


def validate_secret(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(character.isspace() for character in value):
        raise WeComProviderError("invalid_credential", "WeCom bot secret is invalid")
    return value


def generate_pairing_code() -> str:
    return secrets.token_hex(4).upper()


def hash_pairing_code(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def parse_pair_command(text: str) -> str | None:
    parts = text.strip().split()
    if len(parts) != 2 or parts[0].lower() != "/pair" or not 6 <= len(parts[1]) <= 16:
        return None
    if not parts[1].isascii() or not all(character.isalnum() for character in parts[1]):
        return None
    return parts[1].upper()


def parse_frame(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeComProviderError("provider_unavailable", "WeCom returned an unreadable frame") from exc
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > WECOM_MAX_FRAME_BYTES:
        raise WeComProviderError("response_too_large", "WeCom returned an oversized frame")
    try:
        frame = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WeComProviderError("provider_unavailable", "WeCom returned invalid JSON") from exc
    if not isinstance(frame, dict):
        raise WeComProviderError("provider_unavailable", "WeCom returned an invalid frame")
    return frame


def parse_inbound_message(frame: Mapping[str, Any]) -> ParsedWeComMessage | None:
    if frame.get("cmd") != WECOM_CALLBACK_COMMAND:
        return None
    headers = frame.get("headers")
    body = frame.get("body")
    if not isinstance(headers, Mapping) or not isinstance(body, Mapping):
        return None
    request_id = headers.get("req_id")
    message_id = body.get("msgid")
    chat_type = body.get("chattype")
    sender = body.get("from")
    message_type = body.get("msgtype")
    if (
        not isinstance(request_id, str)
        or not request_id
        or not isinstance(message_id, str)
        or not message_id
        or len(message_id) > 256
        or chat_type not in {"single", "group"}
        or not isinstance(sender, Mapping)
        or not isinstance(sender.get("userid"), str)
        or not sender["userid"]
    ):
        return None
    content: Any = None
    if message_type == "text":
        message = body.get("text")
        if isinstance(message, Mapping):
            content = message.get("content")
    elif message_type == "voice":
        message = body.get("voice")
        if isinstance(message, Mapping):
            # WeCom's Intelligent Bot callback provides the recognized text
            # here. Audio without transcription is intentionally unsupported.
            content = message.get("content")
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or len(text.encode("utf-8")) > WECOM_MAX_STREAM_BYTES:
        return None
    return ParsedWeComMessage(
        message_id=message_id,
        request_id=request_id,
        user_id=sender["userid"],
        chat_type=chat_type,
        text=text,
    )


def make_subscribe_frame(bot_id: str, secret: str, *, request_id: str | None = None) -> dict[str, Any]:
    validate_bot_id(bot_id)
    validate_secret(secret)
    return {
        "cmd": WECOM_SUBSCRIBE_COMMAND,
        "headers": {"req_id": request_id or _request_id(WECOM_SUBSCRIBE_COMMAND)},
        "body": {"bot_id": bot_id, "secret": secret},
    }


def make_ping_frame(*, request_id: str | None = None) -> dict[str, Any]:
    return {
        "cmd": WECOM_PING_COMMAND,
        "headers": {"req_id": request_id or _request_id(WECOM_PING_COMMAND)},
    }


def make_response_frame(request_id: str, stream_id: str, content: str, *, finish: bool = True) -> dict[str, Any]:
    _require_request_id(request_id)
    _require_request_id(stream_id)
    if len(content.encode("utf-8")) > WECOM_MAX_STREAM_BYTES:
        raise WeComProviderError("response_too_large", "WeCom response content is too large")
    return {
        "cmd": WECOM_RESPONSE_COMMAND,
        "headers": {"req_id": request_id},
        "body": {
            "msgtype": "stream",
            "stream": {"id": stream_id, "finish": finish, "content": content},
        },
    }


def make_send_frame(user_id: str, content: str, *, request_id: str | None = None) -> dict[str, Any]:
    if not isinstance(user_id, str) or not 1 <= len(user_id) <= 128:
        raise WeComProviderError("invalid_input", "WeCom user ID is invalid")
    if len(content.encode("utf-8")) > WECOM_MAX_STREAM_BYTES:
        raise WeComProviderError("response_too_large", "WeCom response content is too large")
    return {
        "cmd": WECOM_SEND_COMMAND,
        "headers": {"req_id": request_id or _request_id(WECOM_SEND_COMMAND)},
        "body": {
            "chatid": user_id,
            "msgtype": "markdown",
            "markdown": {"content": content},
        },
    }


def chunk_text_for_wecom(text: str) -> list[str]:
    if not isinstance(text, str):
        raise WeComProviderError("invalid_input", "WeCom response text is invalid")
    if not text:
        return [""]
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in text:
        character_bytes = len(character.encode("utf-8"))
        if current and current_bytes + character_bytes > WECOM_MAX_STREAM_BYTES:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    if current:
        chunks.append("".join(current))
    return chunks


def open_wecom_websocket() -> WeComSocket:
    try:
        from websockets.sync.client import connect

        return connect(
            WECOM_WS_URL,
            open_timeout=WECOM_SUBSCRIBE_TIMEOUT_SECONDS,
            close_timeout=5,
            max_size=WECOM_MAX_FRAME_BYTES,
            ping_interval=None,
        )
    except WeComProviderError:
        raise
    except Exception as exc:
        raise WeComProviderError("provider_unavailable", "WeCom WebSocket connection failed") from exc


def _request_id(command: str) -> str:
    return f"{command}-{uuid.uuid4().hex}"


def _require_request_id(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise WeComProviderError("invalid_input", "WeCom request ID is invalid")
