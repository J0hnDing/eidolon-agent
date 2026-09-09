"""Trusted Telegram Bot API adapter and long-polling helpers.

The application owns pairing, approval, and persistence decisions.  This
module deliberately keeps those concerns behind small callbacks so that the
Telegram process cannot bypass the backend approval service.  Provider
credentials are accepted only at the adapter boundary and are never included
in exceptions or returned values.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.services.github_provider import IntegrationProviderError
from app.services.invocation_approval_presentation import (
    ApprovalMessagePresentation,
    ApprovalPresentation,
    initial_message,
    outcome_message,
)

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_API_HOST = "api.telegram.org"
TELEGRAM_LONG_POLL_TIMEOUT_SECONDS = 30
TELEGRAM_MAX_MESSAGE_CHARS = 4096
TELEGRAM_MAX_CALLBACK_DATA_BYTES = 64
TELEGRAM_MAX_APPROVAL_INPUT_BYTES = 32 * 1024
TELEGRAM_MAX_TITLE_CHARS = 120
TELEGRAM_MAX_DESCRIPTION_CHARS = 800
TELEGRAM_MAX_LINK_CHARS = 2048
TELEGRAM_PAIRING_TTL_SECONDS = 600
TELEGRAM_MAX_RESPONSE_BYTES = 2_000_000

PairingDecision = Literal["approve", "deny"]


class TelegramProviderError(IntegrationProviderError):
    """A normalized Telegram provider or protocol failure."""


class TelegramBotApi(Protocol):
    """The narrow Bot API surface used by Eidolon.

    Methods return normalized Telegram result dictionaries.  The protocol is
    intentionally synchronous: the long-poll worker runs in its own thread,
    while the backend's callback can hand work to its normal service layer.
    """

    def get_me(self) -> dict[str, Any]: ...

    def get_webhook_info(self) -> dict[str, Any]: ...

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = TELEGRAM_LONG_POLL_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]: ...

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def send_chat_action(
        self,
        chat_id: int | str,
        action: str,
        *,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]: ...

    def send_message_draft(
        self,
        chat_id: int | str,
        draft_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]: ...

    def create_forum_topic(
        self,
        chat_id: int | str,
        name: str,
        *,
        icon_color: int | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> dict[str, Any]: ...

    def edit_forum_topic(
        self,
        chat_id: int | str,
        message_thread_id: int,
        *,
        name: str | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> Any: ...

    def delete_forum_topic(self, chat_id: int | str, message_thread_id: int) -> Any: ...

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _require_text(value: Any, field: str, *, minimum: int = 1, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise TelegramProviderError("invalid_input", f"{field} must contain {minimum}-{maximum} characters")
    return value


def _require_chat_id(value: Any) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TelegramProviderError("invalid_input", "Telegram chat id is invalid")
    if isinstance(value, str) and not value:
        raise TelegramProviderError("invalid_input", "Telegram chat id is invalid")
    return value


def _require_thread_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TelegramProviderError("invalid_input", "Telegram message thread id is invalid")
    return value


def _validate_topic_name(value: Any, *, allow_empty: bool = False) -> str:
    minimum = 0 if allow_empty else 1
    if not isinstance(value, str) or not minimum <= len(value) <= 128:
        raise TelegramProviderError("invalid_input", "Telegram topic name is invalid")
    return value


def _require_message_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TelegramProviderError("invalid_input", "Telegram message id is invalid")
    return value


def _validate_link_url(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= TELEGRAM_MAX_LINK_CHARS:
        raise TelegramProviderError("invalid_input", "link must contain 1-2048 characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise TelegramProviderError("invalid_input", "link must be an HTTP(S) URL")
    return value


def _error_from_http(code: int) -> TelegramProviderError:
    if code == 401:
        return TelegramProviderError("invalid_credential", "Telegram bot token is invalid")
    if code == 409:
        return TelegramProviderError("polling_conflict", "Another Telegram long-poll request is active")
    if code == 429:
        return TelegramProviderError("rate_limited", "Telegram rate limit was reached")
    if 500 <= code <= 599:
        return TelegramProviderError("provider_unavailable", "Telegram is temporarily unavailable")
    if code == 400:
        return TelegramProviderError("invalid_input", "Telegram rejected the request")
    return TelegramProviderError("provider_unavailable", "Telegram request failed")


class UrllibTelegramBotApi:
    """Fixed-host HTTPS Telegram Bot API client.

    The bot token is held only by this instance.  Request URLs necessarily
    contain the token as required by Telegram, but no URL, token, or response
    body is ever included in an error message.
    """

    def __init__(self, token: str, *, opener: Any | None = None) -> None:
        if (
            not isinstance(token, str)
            or not token.strip()
            or len(token) > 512
            or any(character.isspace() or character in "/?#" for character in token)
        ):
            raise TelegramProviderError("invalid_credential", "Telegram bot token is invalid")
        self._token = token
        self._opener = opener or build_opener(_NoRedirect())

    def get_me(self) -> dict[str, Any]:
        payload = self._request_json("getMe", {}, timeout=15)
        result = payload.get("result")
        if not isinstance(result, dict) or isinstance(result.get("id"), bool) or not isinstance(result.get("id"), int):
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid bot identity")
        username = result.get("username")
        first_name = result.get("first_name")
        if username is not None and (not isinstance(username, str) or len(username) > 64):
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid bot identity")
        if not isinstance(first_name, str) or not first_name or len(first_name) > 256:
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid bot identity")
        topics_enabled = result.get("has_topics_enabled")
        allows_users_to_create_topics = result.get("allows_users_to_create_topics")
        if topics_enabled is not None and not isinstance(topics_enabled, bool):
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid bot identity")
        if allows_users_to_create_topics is not None and not isinstance(allows_users_to_create_topics, bool):
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid bot identity")
        return {
            "id": result["id"],
            "username": username,
            "first_name": first_name,
            "has_topics_enabled": topics_enabled,
            "allows_users_to_create_topics": allows_users_to_create_topics,
        }

    def get_webhook_info(self) -> dict[str, Any]:
        payload = self._request_json("getWebhookInfo", {}, timeout=15)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise TelegramProviderError("provider_unavailable", "Telegram returned invalid webhook status")
        url = result.get("url")
        pending = result.get("pending_update_count", 0)
        if not isinstance(url, str) or not isinstance(pending, int) or pending < 0:
            raise TelegramProviderError("provider_unavailable", "Telegram returned invalid webhook status")
        # The URL is not a secret, but only return the fields needed by the
        # worker and UI.  Telegram may include an opaque IP/error description.
        return {"url": url, "configured": bool(url), "pending_update_count": pending}

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = TELEGRAM_LONG_POLL_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int)):
            raise TelegramProviderError("invalid_input", "Telegram update offset is invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 <= timeout <= 30:
            raise TelegramProviderError("invalid_input", "Telegram long-poll timeout is invalid")
        request: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            request["offset"] = offset
        payload = self._request_json("getUpdates", request, timeout=max(timeout + 5, 10))
        result = payload.get("result")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TelegramProviderError("provider_unavailable", "Telegram returned invalid updates")
        return result

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require_chat_id(chat_id)
        _validate_message_text(text)
        request: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if message_thread_id is not None:
            request["message_thread_id"] = _require_thread_id(message_thread_id)
        if parse_mode is not None:
            _validate_parse_mode(parse_mode)
            request["parse_mode"] = parse_mode
        if reply_markup is not None:
            _validate_reply_markup(reply_markup)
            request["reply_markup"] = reply_markup
        return self._request_json("sendMessage", request, timeout=15).get("result", {})

    def send_chat_action(
        self,
        chat_id: int | str,
        action: str,
        *,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        _require_chat_id(chat_id)
        _require_text(action, "Telegram chat action", maximum=32)
        request: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if message_thread_id is not None:
            request["message_thread_id"] = _require_thread_id(message_thread_id)
        return self._request_json("sendChatAction", request, timeout=10).get("result", {})

    def send_message_draft(
        self,
        chat_id: int | str,
        draft_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        _require_chat_id(chat_id)
        if isinstance(draft_id, bool) or not isinstance(draft_id, int) or draft_id == 0:
            raise TelegramProviderError("invalid_input", "Telegram draft id is invalid")
        if not isinstance(text, str) or len(text) > TELEGRAM_MAX_MESSAGE_CHARS:
            raise TelegramProviderError("invalid_input", "Telegram draft text is invalid")
        request: dict[str, Any] = {"chat_id": chat_id, "draft_id": draft_id, "text": text}
        if message_thread_id is not None:
            request["message_thread_id"] = _require_thread_id(message_thread_id)
        if parse_mode is not None:
            _validate_parse_mode(parse_mode)
            request["parse_mode"] = parse_mode
        return self._request_json("sendMessageDraft", request, timeout=15).get("result", {})

    def create_forum_topic(
        self,
        chat_id: int | str,
        name: str,
        *,
        icon_color: int | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> dict[str, Any]:
        _require_chat_id(chat_id)
        _validate_topic_name(name)
        request: dict[str, Any] = {"chat_id": chat_id, "name": name}
        if icon_color is not None:
            if isinstance(icon_color, bool) or not isinstance(icon_color, int):
                raise TelegramProviderError("invalid_input", "Telegram topic icon color is invalid")
            request["icon_color"] = icon_color
        if icon_custom_emoji_id is not None:
            _require_text(icon_custom_emoji_id, "Telegram topic icon", maximum=256)
            request["icon_custom_emoji_id"] = icon_custom_emoji_id
        result = self._request_json("createForumTopic", request, timeout=15).get("result")
        if not isinstance(result, dict):
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid forum topic")
        thread_id = result.get("message_thread_id")
        result_name = result.get("name")
        if isinstance(thread_id, bool) or not isinstance(thread_id, int) or thread_id < 1:
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid forum topic")
        if not isinstance(result_name, str) or not 1 <= len(result_name) <= 128:
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid forum topic")
        return result

    def edit_forum_topic(
        self,
        chat_id: int | str,
        message_thread_id: int,
        *,
        name: str | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> Any:
        _require_chat_id(chat_id)
        _require_thread_id(message_thread_id)
        request: dict[str, Any] = {"chat_id": chat_id, "message_thread_id": message_thread_id}
        if name is not None:
            _validate_topic_name(name, allow_empty=True)
            request["name"] = name
        if icon_custom_emoji_id is not None:
            if not isinstance(icon_custom_emoji_id, str) or len(icon_custom_emoji_id) > 256:
                raise TelegramProviderError("invalid_input", "Telegram topic icon is invalid")
            request["icon_custom_emoji_id"] = icon_custom_emoji_id
        return self._request_json("editForumTopic", request, timeout=15).get("result")

    def delete_forum_topic(self, chat_id: int | str, message_thread_id: int) -> Any:
        _require_chat_id(chat_id)
        _require_thread_id(message_thread_id)
        return self._request_json(
            "deleteForumTopic",
            {"chat_id": chat_id, "message_thread_id": message_thread_id},
            timeout=15,
        ).get("result")

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require_chat_id(chat_id)
        _require_message_id(message_id)
        _validate_message_text(text)
        request: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode is not None:
            _validate_parse_mode(parse_mode)
            request["parse_mode"] = parse_mode
        if reply_markup is not None:
            _validate_reply_markup(reply_markup)
            request["reply_markup"] = reply_markup
        return self._request_json("editMessageText", request, timeout=15).get("result", {})

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        _require_text(callback_query_id, "callback query id", maximum=256)
        if not isinstance(show_alert, bool):
            raise TelegramProviderError("invalid_input", "callback alert flag is invalid")
        request: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text is not None:
            request["text"] = _require_text(text, "callback answer", maximum=200)
        return self._request_json("answerCallbackQuery", request, timeout=10).get("result", {})

    def _request_json(self, method: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]+", method):
            raise TelegramProviderError("internal_failure", "Telegram method is invalid")
        url = f"{TELEGRAM_API_BASE}/bot{self._token}/{method}"
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != TELEGRAM_API_HOST:
            raise TelegramProviderError("internal_failure", "Telegram provider URL is outside the trusted boundary")
        request = Request(
            url,
            data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(TELEGRAM_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise _error_from_http(exc.code) from None
        except (TimeoutError, URLError, OSError):
            raise TelegramProviderError("provider_timeout", "Telegram did not respond before the timeout") from None
        if len(raw) > TELEGRAM_MAX_RESPONSE_BYTES:
            raise TelegramProviderError("response_too_large", "Telegram response exceeded the operation limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid response") from None
        if not isinstance(value, dict) or value.get("ok") is not True:
            error_code = value.get("error_code") if isinstance(value, dict) else None
            if isinstance(error_code, int):
                raise _error_from_http(error_code)
            raise TelegramProviderError("provider_unavailable", "Telegram returned an unsuccessful response")
        return value


def _validate_parse_mode(value: Any) -> None:
    if value not in {"HTML", None}:
        raise TelegramProviderError("invalid_input", "Telegram parse mode is unsupported")


def _validate_message_text(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= TELEGRAM_MAX_MESSAGE_CHARS:
        raise TelegramProviderError("invalid_input", "Telegram message text exceeds the 4096 character limit")
    return value


def _validate_reply_markup(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"inline_keyboard"}:
        raise TelegramProviderError("invalid_input", "Telegram reply markup is invalid")
    keyboard = value.get("inline_keyboard")
    if not isinstance(keyboard, list) or any(not isinstance(row, list) or not row for row in keyboard):
        raise TelegramProviderError("invalid_input", "Telegram reply markup is invalid")
    for row in keyboard:
        for button in row:
            if not isinstance(button, dict) or not isinstance(button.get("text"), str):
                raise TelegramProviderError("invalid_input", "Telegram reply markup is invalid")
            if "callback_data" in button:
                callback_data = button["callback_data"]
                if not isinstance(callback_data, str) or len(callback_data.encode("utf-8")) > TELEGRAM_MAX_CALLBACK_DATA_BYTES:
                    raise TelegramProviderError("invalid_input", "Telegram callback data exceeds the 64 byte limit")
            elif "url" in button:
                _validate_link_url(button["url"])
            else:
                raise TelegramProviderError("invalid_input", "Telegram reply markup button is invalid")


@dataclass(frozen=True)
class PairingCode:
    """Plain pairing code plus the hash safe to persist in the database."""

    code: str
    code_hash: str
    expires_at: float


def hash_pairing_code(code: str) -> str:
    _require_text(code, "pairing code", maximum=128)
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def create_pairing_code(*, ttl_seconds: int = TELEGRAM_PAIRING_TTL_SECONDS, now: float | None = None) -> PairingCode:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 3600:
        raise TelegramProviderError("invalid_input", "Pairing code lifetime is invalid")
    code = secrets.token_urlsafe(24)
    return PairingCode(
        code=code,
        code_hash=hash_pairing_code(code),
        expires_at=(time.time() if now is None else now) + ttl_seconds,
    )


def parse_start_command(text: Any) -> str | None:
    """Return a bounded ``/start`` code, accepting Telegram's bot suffix."""

    if not isinstance(text, str) or len(text) > 512:
        return None
    parts = text.strip().split()
    if len(parts) != 2 or not re.fullmatch(r"/start(?:@[A-Za-z0-9_]{1,64})?", parts[0], re.IGNORECASE):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", parts[1]):
        return None
    return parts[1]


@dataclass(frozen=True)
class PairingMessage:
    update_id: int
    code: str
    chat_id: int
    user_id: int


def parse_pairing_update(update: Mapping[str, Any]) -> PairingMessage | None:
    if not isinstance(update, Mapping):
        return None
    update_id = update.get("update_id")
    message = update.get("message")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or not isinstance(message, Mapping):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, Mapping) or chat.get("type") != "private":
        return None
    chat_id = chat.get("id")
    user_id = sender.get("id") if isinstance(sender, Mapping) else None
    code = parse_start_command(message.get("text"))
    if (
        isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or code is None
    ):
        return None
    return PairingMessage(update_id=update_id, code=code, chat_id=chat_id, user_id=user_id)


def validate_pairing_message(
    update: Mapping[str, Any],
    expected_code_hash: str,
    *,
    expires_at: float | None = None,
    expected_chat_id: int | None = None,
    expected_user_id: int | None = None,
    now: float | None = None,
) -> PairingMessage:
    pairing = parse_pairing_update(update)
    if pairing is None:
        raise TelegramProviderError("invalid_pairing", "Telegram pairing must come from a private /start message")
    current = time.time() if now is None else now
    if expires_at is not None and current >= expires_at:
        raise TelegramProviderError("invalid_pairing", "Telegram pairing code is expired")
    if not isinstance(expected_code_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_code_hash):
        raise TelegramProviderError("invalid_pairing", "Telegram pairing code is invalid")
    if not secrets.compare_digest(hash_pairing_code(pairing.code), expected_code_hash):
        raise TelegramProviderError("invalid_pairing", "Telegram pairing code is invalid")
    if expected_chat_id is not None and pairing.chat_id != expected_chat_id:
        raise TelegramProviderError("authorization_missing_or_stale", "Telegram pairing chat is not authorized")
    if expected_user_id is not None and pairing.user_id != expected_user_id:
        raise TelegramProviderError("authorization_missing_or_stale", "Telegram pairing user is not authorized")
    return pairing


class PairingCodeStore:
    """Small deterministic helper for one-time pairing in tests or adapters.

    Production persistence should store only ``PairingCode.code_hash`` and
    consume it transactionally.  This class is deliberately not a replacement
    for the application's database row.
    """

    def __init__(self, *, ttl_seconds: int = TELEGRAM_PAIRING_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._pending: PairingCode | None = None
        self._lock = threading.Lock()

    def create(self, *, now: float | None = None) -> PairingCode:
        code = create_pairing_code(ttl_seconds=self.ttl_seconds, now=now)
        with self._lock:
            self._pending = code
        return code

    def consume(
        self,
        update: Mapping[str, Any],
        *,
        expected_chat_id: int | None = None,
        expected_user_id: int | None = None,
        now: float | None = None,
    ) -> PairingMessage:
        with self._lock:
            pending = self._pending
        if pending is None:
            raise TelegramProviderError("invalid_pairing", "Telegram pairing code is invalid or already used")
        pairing = validate_pairing_message(
            update,
            pending.code_hash,
            expires_at=pending.expires_at,
            expected_chat_id=expected_chat_id,
            expected_user_id=expected_user_id,
            now=now,
        )
        with self._lock:
            # Consume only a successful, authorized private message.  A bad
            # code/chat/user must not let an attacker burn the real pairing
            # code, while the identity check makes concurrent replays one-use.
            if self._pending is not pending:
                raise TelegramProviderError("invalid_pairing", "Telegram pairing code is invalid or already used")
            self._pending = None
        return pairing


@dataclass(frozen=True)
class TelegramApprovalCallback:
    update_id: int
    callback_query_id: str
    approval_id: int
    decision: PairingDecision
    nonce: str
    chat_id: int
    user_id: int
    message_id: int | None
    kind: Literal["invocation", "proposal"] = "invocation"


_CALLBACK_RE = re.compile(r"^(approve|deny):([1-9][0-9]{0,18}):([A-Za-z0-9_-]{8,32})$")
_PROPOSAL_CALLBACK_RE = re.compile(
    r"^proposal:(approve|deny):([1-9][0-9]{0,18}):([A-Za-z0-9_-]{8,32})$"
)


def create_callback_nonce() -> str:
    return secrets.token_urlsafe(12)


def hash_callback_nonce(nonce: str) -> str:
    _require_text(nonce, "callback nonce", maximum=64)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", nonce):
        raise TelegramProviderError("invalid_input", "callback nonce is invalid")
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def build_callback_data(approval_id: int, decision: PairingDecision, nonce: str | None = None) -> tuple[str, str]:
    if isinstance(approval_id, bool) or not isinstance(approval_id, int) or not 1 <= approval_id <= 10**18:
        raise TelegramProviderError("invalid_input", "approval id is invalid")
    if decision not in {"approve", "deny"}:
        raise TelegramProviderError("invalid_input", "approval decision is invalid")
    callback_nonce = nonce or create_callback_nonce()
    digest = hash_callback_nonce(callback_nonce)
    value = f"{decision}:{approval_id}:{callback_nonce}"
    if len(value.encode("utf-8")) > TELEGRAM_MAX_CALLBACK_DATA_BYTES:
        raise TelegramProviderError("invalid_input", "Telegram callback data exceeds the 64 byte limit")
    return value, digest


def build_proposal_callback_data(
    proposal_id: int,
    decision: PairingDecision,
    nonce: str | None = None,
) -> tuple[str, str]:
    if isinstance(proposal_id, bool) or not isinstance(proposal_id, int) or not 1 <= proposal_id <= 10**18:
        raise TelegramProviderError("invalid_input", "proposal id is invalid")
    if decision not in {"approve", "deny"}:
        raise TelegramProviderError("invalid_input", "proposal decision is invalid")
    callback_nonce = nonce or create_callback_nonce()
    digest = hash_callback_nonce(callback_nonce)
    value = f"proposal:{decision}:{proposal_id}:{callback_nonce}"
    if len(value.encode("utf-8")) > TELEGRAM_MAX_CALLBACK_DATA_BYTES:
        raise TelegramProviderError("invalid_input", "Telegram callback data exceeds the 64 byte limit")
    return value, digest


def parse_callback_data(data: Any) -> tuple[PairingDecision, int, str] | None:
    if not isinstance(data, str) or len(data.encode("utf-8")) > TELEGRAM_MAX_CALLBACK_DATA_BYTES:
        return None
    match = _CALLBACK_RE.fullmatch(data)
    if match is None:
        return None
    decision = match.group(1)
    approval_id = int(match.group(2))
    nonce = match.group(3)
    return decision, approval_id, nonce  # type: ignore[return-value]


def parse_callback_update(update: Mapping[str, Any]) -> TelegramApprovalCallback | None:
    if not isinstance(update, Mapping):
        return None
    update_id = update.get("update_id")
    query = update.get("callback_query")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or not isinstance(query, Mapping):
        return None
    query_id = query.get("id")
    sender = query.get("from")
    message = query.get("message")
    raw_data = query.get("data")
    data = parse_callback_data(raw_data)
    callback_kind: Literal["invocation", "proposal"] = "invocation"
    if data is None and isinstance(raw_data, str) and len(raw_data.encode("utf-8")) <= TELEGRAM_MAX_CALLBACK_DATA_BYTES:
        proposal_match = _PROPOSAL_CALLBACK_RE.fullmatch(raw_data)
        if proposal_match is not None:
            data = (
                proposal_match.group(1),
                int(proposal_match.group(2)),
                proposal_match.group(3),
            )  # type: ignore[assignment]
            callback_kind = "proposal"
    if (
        not isinstance(query_id, str)
        or not query_id
        or not isinstance(sender, Mapping)
        or isinstance(sender.get("id"), bool)
        or not isinstance(sender.get("id"), int)
        or not isinstance(message, Mapping)
        or not isinstance(message.get("chat"), Mapping)
        or isinstance(message["chat"].get("id"), bool)
        or not isinstance(message["chat"].get("id"), int)
        or data is None
    ):
        return None
    chat = message["chat"]
    message_id = message.get("message_id")
    if message_id is not None and (isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 1):
        message_id = None
    decision, approval_id, nonce = data
    return TelegramApprovalCallback(
        update_id=update_id,
        callback_query_id=query_id,
        approval_id=approval_id,
        decision=decision,
        nonce=nonce,
        chat_id=chat["id"],
        user_id=sender["id"],
        message_id=message_id,
        kind=callback_kind,
    )


def validate_callback_origin(
    callback: TelegramApprovalCallback,
    *,
    expected_chat_id: int,
    expected_user_id: int,
    expected_nonce_hash: str | None = None,
) -> None:
    if callback.chat_id != expected_chat_id or callback.user_id != expected_user_id:
        raise TelegramProviderError("authorization_missing_or_stale", "Telegram approval origin is not authorized")
    if expected_nonce_hash is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_nonce_hash) or not secrets.compare_digest(
            hash_callback_nonce(callback.nonce), expected_nonce_hash
        ):
            raise TelegramProviderError("authorization_missing_or_stale", "Telegram approval callback is stale")


@dataclass(frozen=True)
class ApprovalDelivery:
    message_ids: tuple[int, ...]
    nonce: str
    nonce_hash: str
    approve_callback_data: str
    deny_callback_data: str


def render_approval_text(presentation: ApprovalPresentation) -> str:
    if isinstance(presentation.approval_id, bool) or not isinstance(presentation.approval_id, int) or presentation.approval_id < 1:
        raise TelegramProviderError("invalid_input", "approval id is invalid")
    message = initial_message(presentation)
    lines = [message.heading]
    if message.lead:
        lines.append(message.lead)
    for field in message.fields:
        separator = "\n" if field.multiline else " "
        lines.append(f"{field.label}:{separator}{field.value}")
    return "\n".join(lines)


def _split_escaped_text(text: str, max_chars: int) -> list[str]:
    if not isinstance(text, str) or not text:
        raise TelegramProviderError("invalid_input", "Telegram message text cannot be empty")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise TelegramProviderError("invalid_input", "Telegram message chunk size is invalid")
    chunks: list[str] = []
    raw_chunk: list[str] = []
    escaped_length = 0
    for character in text:
        escaped = html.escape(character, quote=False)
        if raw_chunk and escaped_length + len(escaped) > max_chars:
            chunks.append("".join(raw_chunk))
            raw_chunk = []
            escaped_length = 0
        if len(escaped) > max_chars:
            raise TelegramProviderError("invalid_input", "Telegram message contains an unsupported character")
        raw_chunk.append(character)
        escaped_length += len(escaped)
    if raw_chunk:
        chunks.append("".join(raw_chunk))
    return chunks


def chunk_text_for_telegram(text: str, *, max_chars: int = TELEGRAM_MAX_MESSAGE_CHARS) -> list[str]:
    """Split text so its HTML-escaped form fits Telegram's message limit."""

    return _split_escaped_text(text, max_chars)


def _approval_markup(approval_id: int, nonce: str) -> dict[str, Any]:
    approve, _ = build_callback_data(approval_id, "approve", nonce)
    deny, _ = build_callback_data(approval_id, "deny", nonce)
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": approve},
            {"text": "❌ Deny", "callback_data": deny},
        ]]
    }
    _validate_reply_markup(markup)
    return markup


def send_approval_request(
    api: TelegramBotApi,
    chat_id: int | str,
    presentation: ApprovalPresentation,
    *,
    nonce: str | None = None,
    message_thread_id: int | None = None,
) -> ApprovalDelivery:
    """Send a bounded approval preview with controls on its status message."""

    _require_chat_id(chat_id)
    render_approval_text(presentation)
    html_chunks = _message_html_chunks(initial_message(presentation))
    callback_nonce = nonce or create_callback_nonce()
    nonce_hash = hash_callback_nonce(callback_nonce)
    markup = _approval_markup(presentation.approval_id, callback_nonce)
    message_ids: list[int] = []
    for index, text in enumerate(html_chunks):
        _validate_message_text(text)
        result = api.send_message(
            chat_id,
            text,
            message_thread_id=message_thread_id,
            parse_mode="HTML",
            reply_markup=markup if index == 0 else None,
        )
        message_id = result.get("message_id") if isinstance(result, Mapping) else None
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 1:
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid message id")
        message_ids.append(message_id)
    approve, _ = build_callback_data(presentation.approval_id, "approve", callback_nonce)
    deny, _ = build_callback_data(presentation.approval_id, "deny", callback_nonce)
    return ApprovalDelivery(
        message_ids=tuple(message_ids),
        nonce=callback_nonce,
        nonce_hash=nonce_hash,
        approve_callback_data=approve,
        deny_callback_data=deny,
    )


def _agent_proposal_html_chunks(
    *,
    heading: str,
    title: str,
    rationale: str,
    instruction: str,
    actions: str,
    references: list[str],
) -> list[str]:
    fields = (
        ("Title", title),
        ("Why", rationale),
        ("Proposed actions", actions),
        ("Act instruction", instruction),
        ("Related goals and todos", "\n".join(references) if references else "None"),
    )
    fragments = [heading]
    for label, raw_value in fields:
        prefix = f"<b>{html.escape(label, quote=False)}:</b>\n"
        value = raw_value or "None"
        available = max(1, TELEGRAM_MAX_MESSAGE_CHARS - len(prefix))
        value_chunks = _split_escaped_text(value, available)
        fragments.append(prefix + html.escape(value_chunks[0], quote=False))
        fragments.extend(html.escape(chunk, quote=False) for chunk in value_chunks[1:])
    chunks: list[str] = []
    current = ""
    for fragment in fragments:
        candidate = fragment if not current else f"{current}\n{fragment}"
        if current and len(candidate) > TELEGRAM_MAX_MESSAGE_CHARS:
            chunks.append(current)
            current = fragment
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_agent_proposal_request(
    api: TelegramBotApi,
    chat_id: int | str,
    *,
    proposal_id: int,
    title: str,
    rationale: str,
    instruction: str,
    actions: str,
    references: list[str],
    nonce: str | None = None,
    message_thread_id: int | None = None,
) -> ApprovalDelivery:
    """Send an Assistant proposal with complete Act instructions and opaque controls."""

    _require_chat_id(chat_id)
    callback_nonce = nonce or create_callback_nonce()
    nonce_hash = hash_callback_nonce(callback_nonce)
    approve, _ = build_proposal_callback_data(proposal_id, "approve", callback_nonce)
    deny, _ = build_proposal_callback_data(proposal_id, "deny", callback_nonce)
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve plan", "callback_data": approve},
            {"text": "❌ Deny", "callback_data": deny},
        ]]
    }
    _validate_reply_markup(markup)
    chunks = _agent_proposal_html_chunks(
        heading="🤔 <b>Assistant plan approval</b>",
        title=title,
        rationale=rationale,
        instruction=instruction,
        actions=actions,
        references=references,
    )
    message_ids: list[int] = []
    for index, text in enumerate(chunks):
        _validate_message_text(text)
        result = api.send_message(
            chat_id,
            text,
            message_thread_id=message_thread_id,
            parse_mode="HTML",
            reply_markup=markup if index == 0 else None,
        )
        message_id = result.get("message_id") if isinstance(result, Mapping) else None
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 1:
            raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid message id")
        message_ids.append(message_id)
    return ApprovalDelivery(
        message_ids=tuple(message_ids),
        nonce=callback_nonce,
        nonce_hash=nonce_hash,
        approve_callback_data=approve,
        deny_callback_data=deny,
    )


def edit_agent_proposal_outcome(
    api: TelegramBotApi,
    chat_id: int | str,
    message_id: int,
    *,
    title: str,
    rationale: str,
    instruction: str,
    actions: str,
    references: list[str],
    status: str,
    execution_status: str | None,
) -> None:
    """Replace the Assistant proposal status message and remove decision controls."""

    _require_chat_id(chat_id)
    _require_message_id(message_id)
    if status == "denied":
        heading = "❌ <b>Assistant plan denied</b>"
    elif execution_status == "succeeded":
        heading = "✅ <b>Approved · Act completed</b>"
    elif execution_status in {"failed", "interrupted"}:
        heading = "⚠️ <b>Approved · Act did not complete</b>"
    elif execution_status == "running":
        heading = "⏳ <b>Approved · Act is working</b>"
    else:
        heading = "✅ <b>Approved · Act queued</b>"
    text = _agent_proposal_html_chunks(
        heading=heading,
        title=title,
        rationale=rationale,
        instruction=instruction,
        actions=actions,
        references=references,
    )[0]
    api.edit_message_text(
        chat_id,
        message_id,
        text,
        parse_mode="HTML",
        reply_markup={"inline_keyboard": []},
    )


def edit_approval_outcome(
    api: TelegramBotApi,
    chat_id: int | str,
    message_id: int,
    presentation: ApprovalPresentation,
    *,
    decision_status: str,
    execution_status: str,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    """Replace the approval status message and remove its decision controls."""

    _require_chat_id(chat_id)
    _require_message_id(message_id)
    message = outcome_message(
        presentation,
        decision_status=decision_status,
        execution_status=execution_status,
        error_type=error_type,
        error_message=error_message,
    )
    text = _message_html_chunks(message)[0]
    api.edit_message_text(
        chat_id,
        message_id,
        text,
        parse_mode="HTML",
        reply_markup={"inline_keyboard": []},
    )


def _message_html_chunks(message: ApprovalMessagePresentation) -> list[str]:
    marker, separator, heading = message.heading.partition(" ")
    if separator:
        rendered_heading = (
            f"{html.escape(marker, quote=False)} "
            f"<b>{html.escape(heading, quote=False)}</b>"
        )
    else:
        rendered_heading = f"<b>{html.escape(message.heading, quote=False)}</b>"
    fragments: list[str] = [rendered_heading]
    if message.lead:
        fragments.append(html.escape(message.lead, quote=False))
    for field in message.fields:
        label = html.escape(field.label, quote=False)
        prefix = f"<b>{label}:</b>"
        if field.multiline:
            prefix += "\n"
        else:
            prefix += " "
        raw_value = field.value or "None"
        if field.code:
            escaped_value = html.escape(raw_value, quote=False)
            candidate = f"{prefix}<code>{escaped_value}</code>"
            if len(candidate) <= TELEGRAM_MAX_MESSAGE_CHARS:
                fragments.append(candidate)
                continue
        available = max(1, TELEGRAM_MAX_MESSAGE_CHARS - len(prefix))
        value_chunks = _split_escaped_text(raw_value, available)
        fragments.append(prefix + html.escape(value_chunks[0], quote=False))
        fragments.extend(html.escape(chunk, quote=False) for chunk in value_chunks[1:])

    chunks: list[str] = []
    current = ""
    for fragment in fragments:
        candidate = fragment if not current else f"{current}\n{fragment}"
        if current and len(candidate) > TELEGRAM_MAX_MESSAGE_CHARS:
            chunks.append(current)
            current = fragment
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def validate_notification(
    *,
    title: Any,
    description: Any,
    link: Any = None,
    alert: Any = False,
) -> tuple[str, str, str | None, bool]:
    normalized_title = _require_text(title, "title", maximum=TELEGRAM_MAX_TITLE_CHARS)
    normalized_description = _require_text(description, "description", maximum=TELEGRAM_MAX_DESCRIPTION_CHARS)
    normalized_link = None if link is None else _validate_link_url(link)
    if not isinstance(alert, bool):
        raise TelegramProviderError("invalid_input", "Telegram notification alert flag is invalid")
    return normalized_title, normalized_description, normalized_link, alert


def send_notification(
    api: TelegramBotApi,
    chat_id: int | str,
    *,
    title: Any,
    description: Any,
    link: Any = None,
    alert: Any = False,
    message_thread_id: int | None = None,
) -> dict[str, Any]:
    """Send a bounded HTML-escaped notification or alert with an optional URL button."""

    _require_chat_id(chat_id)
    normalized_title, normalized_description, normalized_link, normalized_alert = validate_notification(
        title=title, description=description, link=link, alert=alert
    )
    marker = "❗" if normalized_alert else "🔔"
    text = (
        f"{marker} <b>{html.escape(normalized_title, quote=False)}</b>\n"
        f"{html.escape(normalized_description, quote=False)}"
    )
    _validate_message_text(text)
    markup = None
    if normalized_link is not None:
        markup = {"inline_keyboard": [[{"text": "Open link", "url": normalized_link}]]}
    result = api.send_message(
        chat_id,
        text,
        message_thread_id=message_thread_id,
        parse_mode="HTML",
        reply_markup=markup,
    )
    message_id = result.get("message_id") if isinstance(result, Mapping) else None
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 1:
        raise TelegramProviderError("provider_unavailable", "Telegram returned an invalid message id")
    return {"sent": True, "message_id": message_id}


def validate_bot_token(
    token: str,
    *,
    api_factory: Callable[[str], TelegramBotApi] = UrllibTelegramBotApi,
) -> dict[str, Any]:
    """Call Telegram ``getMe`` and return only its normalized bot identity."""

    return api_factory(token).get_me()


class TelegramNotificationProviderAdapter:
    """Integration-shaped adapter for the sole user-callable Telegram function."""

    def __init__(self, api_factory: Callable[[str], TelegramBotApi] = UrllibTelegramBotApi) -> None:
        self.api_factory = api_factory

    def validate_credential(self, credential: str) -> dict[str, Any]:
        return self.api_factory(credential).get_me()

    def execute(
        self,
        operation: Any,
        input_json: dict[str, Any],
        credential: str,
        *,
        chat_id: int | str | None = None,
    ) -> dict[str, Any]:
        operation_id = getattr(operation, "operation_id", operation)
        if operation_id != "telegram.notification.send":
            raise TelegramProviderError("operation_undeclared", "Telegram operation is unsupported")
        if not isinstance(input_json, dict):
            raise TelegramProviderError("invalid_input", "Telegram notification input is invalid")
        target_chat_id = chat_id if chat_id is not None else input_json.get("chat_id")
        if target_chat_id is None:
            raise TelegramProviderError("connection_unavailable", "Telegram notification chat is not paired")
        return send_notification(
            self.api_factory(credential),
            target_chat_id,
            title=input_json.get("title"),
            description=input_json.get("description"),
            link=input_json.get("link"),
            alert=input_json.get("alert", False),
        )


class FakeTelegramNotificationProviderAdapter(TelegramNotificationProviderAdapter):
    """Integration-shaped fake that keeps all calls in a fake Bot API."""

    def __init__(self, api: FakeTelegramBotApi | None = None) -> None:
        self.api = api or FakeTelegramBotApi()
        super().__init__(lambda _token: self.api)


# Keep provider naming parallel with the existing GitHub/Google adapters.
UrllibTelegramProviderAdapter = TelegramNotificationProviderAdapter
FakeTelegramProviderAdapter = FakeTelegramNotificationProviderAdapter


class TelegramOffsetStore(Protocol):
    def get_offset(self) -> int | None: ...

    def set_offset(self, offset: int) -> None: ...


class InMemoryTelegramOffsetStore:
    def __init__(self, offset: int | None = None) -> None:
        self.offset = offset

    def get_offset(self) -> int | None:
        return self.offset

    def set_offset(self, offset: int) -> None:
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TelegramProviderError("invalid_input", "Telegram update offset is invalid")
        self.offset = offset


PairingHandler = Callable[[PairingMessage], Any]
TextMessageHandler = Callable[[dict[str, Any]], Any]
CallbackHandler = Callable[[TelegramApprovalCallback], Any]
ErrorHandler = Callable[[Exception], Any]


class TelegramLongPollWorker:
    """Lifespan-owned worker for pairing, callbacks, and topic messages."""

    def __init__(
        self,
        api: TelegramBotApi,
        *,
        offset_store: TelegramOffsetStore | None = None,
        expected_chat_id: int | None = None,
        expected_user_id: int | None = None,
        on_pairing: PairingHandler | None = None,
        on_callback: CallbackHandler | None = None,
        on_message: TextMessageHandler | None = None,
        on_error: ErrorHandler | None = None,
        poll_timeout: int = TELEGRAM_LONG_POLL_TIMEOUT_SECONDS,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        if isinstance(poll_timeout, bool) or not isinstance(poll_timeout, int) or not 0 <= poll_timeout <= 30:
            raise TelegramProviderError("invalid_input", "Telegram long-poll timeout is invalid")
        if not 1 <= max_backoff_seconds <= 300:
            raise TelegramProviderError("invalid_input", "Telegram worker backoff is invalid")
        self.api = api
        self.offset_store = offset_store or InMemoryTelegramOffsetStore()
        self.expected_chat_id = expected_chat_id
        self.expected_user_id = expected_user_id
        self.on_pairing = on_pairing
        self.on_callback = on_callback
        self.on_message = on_message
        self.on_error = on_error
        self.poll_timeout = poll_timeout
        self.max_backoff_seconds = max_backoff_seconds

    def ensure_long_polling_ready(self) -> None:
        status = self.api.get_webhook_info()
        if not isinstance(status, Mapping):
            raise TelegramProviderError("provider_unavailable", "Telegram webhook status is invalid")
        if bool(status.get("configured")) or bool(status.get("url")):
            raise TelegramProviderError("webhook_conflict", "Telegram webhook is configured; long polling is disabled")

    def poll_once(self) -> int:
        offset = self.offset_store.get_offset()
        updates = self.api.get_updates(offset=offset, timeout=self.poll_timeout)
        if not isinstance(updates, list):
            raise TelegramProviderError("provider_unavailable", "Telegram returned invalid updates")
        processed = 0
        for update in updates:
            if not isinstance(update, Mapping):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, bool) or not isinstance(update_id, int):
                continue
            try:
                self.process_update(update)
            except Exception as exc:  # noqa: BLE001 - one malformed callback must not poison polling.
                self._report_error(exc)
            # Advance even for an ignored/replayed update.  Approval handling
            # is idempotent in the backend and Telegram update offsets are
            # persisted so a restart does not replay an entire batch.
            self.offset_store.set_offset(update_id + 1)
            processed += 1
        return processed

    def process_update(self, update: Mapping[str, Any]) -> None:
        pairing = parse_pairing_update(update)
        if pairing is not None:
            if self.on_pairing is not None:
                self.on_pairing(pairing)
            return
        message = update.get("message")
        if isinstance(message, Mapping) and self.on_message is not None:
            chat = message.get("chat")
            sender = message.get("from")
            if isinstance(chat, Mapping) and isinstance(sender, Mapping):
                self.on_message(
                    {
                        "chat_id": chat.get("id"),
                        "user_id": sender.get("id"),
                        "text": message.get("text", ""),
                        "message_thread_id": message.get("message_thread_id"),
                        "is_topic_message": message.get("is_topic_message", False),
                        "message_id": message.get("message_id"),
                        "forum_topic_created": message.get("forum_topic_created"),
                        "forum_topic_edited": message.get("forum_topic_edited"),
                        "forum_topic_closed": message.get("forum_topic_closed"),
                        "forum_topic_reopened": message.get("forum_topic_reopened"),
                        # Bot API currently communicates user deletion through
                        # message deletion updates rather than a dedicated
                        # Message service field. Keep this hook for transports
                        # that expose the event directly.
                        "forum_topic_deleted": message.get("forum_topic_deleted"),
                    }
                )
            return
        callback = parse_callback_update(update)
        if callback is None:
            return
        # A callback answer is sent before invoking backend decision logic so
        # Telegram's client does not keep spinning while the local operation
        # is being claimed or revalidated.
        if self.expected_chat_id is not None and callback.chat_id != self.expected_chat_id:
            self._answer_callback(callback.callback_query_id, "This approval is not authorized.", show_alert=True)
            return
        if self.expected_user_id is not None and callback.user_id != self.expected_user_id:
            self._answer_callback(callback.callback_query_id, "This approval is not authorized.", show_alert=True)
            return
        self._answer_callback(callback.callback_query_id, "Processing approval…")
        if self.on_callback is not None:
            self.on_callback(callback)

    def run_forever(self, stop_event: threading.Event) -> None:
        self.ensure_long_polling_ready()
        failures = 0
        while not stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - provider failures back off and retry.
                self._report_error(exc)
                delay = min(self.max_backoff_seconds, float(2**min(failures, 6)))
                failures += 1
                stop_event.wait(delay)
            else:
                failures = 0

    def _answer_callback(self, callback_query_id: str, text: str, *, show_alert: bool = False) -> None:
        try:
            self.api.answer_callback_query(callback_query_id, text=text, show_alert=show_alert)
        except Exception as exc:  # noqa: BLE001 - callback response must not stop polling.
            self._report_error(exc)

    def _report_error(self, error: Exception) -> None:
        if self.on_error is not None:
            try:
                self.on_error(error)
            except Exception:
                pass


class FakeTelegramBotApi:
    """Deterministic in-memory Bot API for provider and worker tests."""

    def __init__(
        self,
        *,
        bot_id: int = 1001,
        username: str = "eidolon_test_bot",
        first_name: str = "Eidolon",
        webhook_url: str = "",
    ) -> None:
        self.bot = {"id": bot_id, "username": username, "first_name": first_name}
        self.webhook_url = webhook_url
        self.updates: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.next_message_id = 1
        self.errors: list[Exception] = []
        self.next_thread_id = 100
        self.topics: dict[int, dict[str, Any]] = {}

    def get_me(self) -> dict[str, Any]:
        self.calls.append(("getMe", {}))
        return dict(self.bot)

    def get_webhook_info(self) -> dict[str, Any]:
        self.calls.append(("getWebhookInfo", {}))
        return {"url": self.webhook_url, "configured": bool(self.webhook_url), "pending_update_count": len(self.updates)}

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = TELEGRAM_LONG_POLL_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        self._maybe_error()
        self.calls.append(("getUpdates", {"offset": offset, "timeout": timeout}))
        return [
            dict(update)
            for update in self.updates
            if offset is None or isinstance(update.get("update_id"), int) and update["update_id"] >= offset
        ]

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._maybe_error()
        _require_chat_id(chat_id)
        _validate_message_text(text)
        if parse_mode is not None:
            _validate_parse_mode(parse_mode)
        if reply_markup is not None:
            _validate_reply_markup(reply_markup)
        message = {
            "message_id": self.next_message_id,
            "chat": {"id": chat_id},
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
            "message_thread_id": message_thread_id,
        }
        self.next_message_id += 1
        self.sent_messages.append(message)
        self.calls.append(("sendMessage", dict(message)))
        return dict(message)

    def send_chat_action(
        self,
        chat_id: int | str,
        action: str,
        *,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        self._maybe_error()
        _require_chat_id(chat_id)
        _require_text(action, "Telegram chat action", maximum=32)
        result = {"chat_id": chat_id, "action": action, "message_thread_id": message_thread_id}
        if message_thread_id is not None:
            _require_thread_id(message_thread_id)
        self.calls.append(("sendChatAction", dict(result)))
        return {"ok": True}

    def send_message_draft(
        self,
        chat_id: int | str,
        draft_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_error()
        _require_chat_id(chat_id)
        if isinstance(draft_id, bool) or not isinstance(draft_id, int) or draft_id == 0:
            raise TelegramProviderError("invalid_input", "Telegram draft id is invalid")
        if not isinstance(text, str) or len(text) > TELEGRAM_MAX_MESSAGE_CHARS:
            raise TelegramProviderError("invalid_input", "Telegram draft text is invalid")
        if message_thread_id is not None:
            _require_thread_id(message_thread_id)
        if parse_mode is not None:
            _validate_parse_mode(parse_mode)
        result = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": text,
            "message_thread_id": message_thread_id,
            "parse_mode": parse_mode,
        }
        self.calls.append(("sendMessageDraft", dict(result)))
        return {"ok": True}

    def create_forum_topic(
        self,
        chat_id: int | str,
        name: str,
        *,
        icon_color: int | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_error()
        _require_chat_id(chat_id)
        _validate_topic_name(name)
        thread_id = self.next_thread_id
        self.next_thread_id += 1
        topic = {
            "message_thread_id": thread_id,
            "name": name,
            "icon_color": icon_color,
            "icon_custom_emoji_id": icon_custom_emoji_id,
        }
        self.topics[thread_id] = dict(topic)
        self.calls.append(
            (
                "createForumTopic",
                {"chat_id": chat_id, "name": name, "icon_color": icon_color, "icon_custom_emoji_id": icon_custom_emoji_id},
            )
        )
        return dict(topic)

    def edit_forum_topic(
        self,
        chat_id: int | str,
        message_thread_id: int,
        *,
        name: str | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> Any:
        self._maybe_error()
        _require_chat_id(chat_id)
        _require_thread_id(message_thread_id)
        if name is not None:
            _validate_topic_name(name, allow_empty=True)
        topic = self.topics.get(message_thread_id)
        if topic is None:
            raise TelegramProviderError("invalid_input", "Telegram topic was not found")
        if name:
            topic["name"] = name
        if icon_custom_emoji_id is not None:
            topic["icon_custom_emoji_id"] = icon_custom_emoji_id
        self.calls.append(
            (
                "editForumTopic",
                {"chat_id": chat_id, "message_thread_id": message_thread_id, "name": name, "icon_custom_emoji_id": icon_custom_emoji_id},
            )
        )
        return True

    def delete_forum_topic(self, chat_id: int | str, message_thread_id: int) -> Any:
        self._maybe_error()
        _require_chat_id(chat_id)
        _require_thread_id(message_thread_id)
        self.topics.pop(message_thread_id, None)
        self.calls.append(("deleteForumTopic", {"chat_id": chat_id, "message_thread_id": message_thread_id}))
        return True

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._maybe_error()
        _require_chat_id(chat_id)
        _require_message_id(message_id)
        _validate_message_text(text)
        if parse_mode is not None:
            _validate_parse_mode(parse_mode)
        if reply_markup is not None:
            _validate_reply_markup(reply_markup)
        result = {
            "message_id": message_id,
            "chat": {"id": chat_id},
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
        }
        self.edited_messages.append(result)
        self.calls.append(("editMessageText", dict(result)))
        return result

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        self._maybe_error()
        _require_text(callback_query_id, "callback query id", maximum=256)
        result = {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert}
        self.answered_callbacks.append(result)
        self.calls.append(("answerCallbackQuery", dict(result)))
        return {"ok": True}

    def _maybe_error(self) -> None:
        if self.errors:
            error = self.errors.pop(0)
            raise error


__all__ = [
    "ApprovalDelivery",
    "ApprovalPresentation",
    "FakeTelegramBotApi",
    "FakeTelegramNotificationProviderAdapter",
    "FakeTelegramProviderAdapter",
    "InMemoryTelegramOffsetStore",
    "PairingCode",
    "PairingCodeStore",
    "PairingMessage",
    "TELEGRAM_API_BASE",
    "TELEGRAM_LONG_POLL_TIMEOUT_SECONDS",
    "TELEGRAM_MAX_APPROVAL_INPUT_BYTES",
    "TELEGRAM_MAX_CALLBACK_DATA_BYTES",
    "TELEGRAM_MAX_DESCRIPTION_CHARS",
    "TELEGRAM_MAX_LINK_CHARS",
    "TELEGRAM_MAX_MESSAGE_CHARS",
    "TELEGRAM_MAX_TITLE_CHARS",
    "TelegramApprovalCallback",
    "TelegramBotApi",
    "TelegramLongPollWorker",
    "TelegramNotificationProviderAdapter",
    "TelegramOffsetStore",
    "TelegramProviderError",
    "UrllibTelegramProviderAdapter",
    "UrllibTelegramBotApi",
    "build_callback_data",
    "build_proposal_callback_data",
    "chunk_text_for_telegram",
    "create_callback_nonce",
    "create_pairing_code",
    "edit_agent_proposal_outcome",
    "edit_approval_outcome",
    "hash_callback_nonce",
    "hash_pairing_code",
    "parse_callback_data",
    "parse_callback_update",
    "parse_pairing_update",
    "parse_start_command",
    "render_approval_text",
    "send_agent_proposal_request",
    "send_approval_request",
    "send_notification",
    "validate_callback_origin",
    "validate_notification",
    "validate_pairing_message",
    "validate_bot_token",
]
