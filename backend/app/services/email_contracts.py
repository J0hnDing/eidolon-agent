"""Shared provider-neutral email contracts and bounded pagination helpers."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

EMAIL_PROVIDERS = ("gmail", "outlook")
MAX_EMAIL_PROVIDERS = 2
MAX_PROVIDER_RESULT_BYTES = 4 * 1024 * 1024
MAX_AGGREGATE_RESULT_BYTES = 8 * 1024 * 1024
MAX_SEARCH_PAGE_SIZE = 25
MAX_READ_NEW_MESSAGES = 50
MAX_CONVERSATION_MESSAGES = 100
COMPOSITE_CURSOR_VERSION = 1


class EmailContractError(ValueError):
    """A caller-visible email contract or cursor error."""

    error_type = "invalid_input"


def utc_rfc3339(value: Any, *, fallback: Any = None) -> str:
    """Normalize provider timestamps to a UTC RFC 3339 string."""

    candidate = value if value not in (None, "") else fallback
    if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
        try:
            parsed = datetime.fromtimestamp(candidate / 1000 if candidate > 10_000_000_000 else candidate, tz=UTC)
        except (OverflowError, OSError, ValueError):
            parsed = None
        if parsed is not None:
            return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(candidate, datetime):
        parsed = candidate
    elif isinstance(candidate, date):
        parsed = datetime(candidate.year, candidate.month, candidate.day, tzinfo=UTC)
    elif isinstance(candidate, str):
        text = candidate.strip()
        if text.isdigit():
            try:
                numeric = float(text)
                parsed = datetime.fromtimestamp(
                    numeric / 1000 if numeric > 10_000_000_000 else numeric,
                    tz=UTC,
                )
            except (OverflowError, OSError, ValueError):
                parsed = None
            if parsed is not None:
                return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is None:
            from email.utils import parsedate_to_datetime

            try:
                parsed = parsedate_to_datetime(candidate)
            except (TypeError, ValueError, IndexError, OverflowError):
                parsed = None
    else:
        parsed = None
    if parsed is None:
        raise EmailContractError("Provider returned an invalid timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp_key(value: Any) -> datetime:
    return datetime.fromisoformat(utc_rfc3339(value).replace("Z", "+00:00"))


def validate_providers(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_EMAIL_PROVIDERS:
        raise EmailContractError("providers must contain one or two email providers")
    if len(value) != len(set(value)) or any(provider not in EMAIL_PROVIDERS for provider in value):
        raise EmailContractError("providers must contain unique Gmail or Outlook provider names")
    return tuple(value)


def validate_provider(value: Any) -> str:
    if value not in EMAIL_PROVIDERS:
        raise EmailContractError("provider must be gmail or outlook")
    return str(value)


def encode_composite_cursor(cursors: Mapping[str, Any], providers: tuple[str, ...]) -> str:
    normalized_providers = tuple(sorted(providers))
    payload = {
        "v": COMPOSITE_CURSOR_VERSION,
        "providers": list(normalized_providers),
        "cursors": {provider: cursors.get(provider) for provider in normalized_providers},
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    if len(encoded) > 2048:
        raise EmailContractError("page token is too large")
    return encoded


def decode_composite_cursor(token: Any, providers: tuple[str, ...]) -> dict[str, Any]:
    if token in (None, ""):
        return {provider: None for provider in providers}
    if not isinstance(token, str) or len(token) > 2048:
        raise EmailContractError("page_token is malformed")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise EmailContractError("page_token is malformed") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "providers", "cursors"}
        or payload.get("v") != COMPOSITE_CURSOR_VERSION
    ):
        raise EmailContractError("page_token version is unsupported")
    expected = list(sorted(providers))
    if payload.get("providers") != expected or not isinstance(payload.get("cursors"), dict):
        raise EmailContractError("page_token does not match the requested providers")
    cursors = payload["cursors"]
    if set(cursors) != set(expected):
        raise EmailContractError("page_token does not match the requested providers")
    normalized: dict[str, Any] = {}
    for provider in expected:
        cursor = cursors[provider]
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048):
            raise EmailContractError("page_token contains an invalid provider cursor")
        normalized[provider] = cursor
    return normalized


def provider_error(
    provider: str,
    error_type: str,
    message: str,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "provider": provider,
        "error_type": error_type[:64],
        "message": message[:500],
    }
    if retry_after_seconds is not None:
        result["retry_after_seconds"] = max(0, min(3600, int(retry_after_seconds)))
    return result


def enforce_budget(value: dict[str, Any], *, maximum: int = MAX_PROVIDER_RESULT_BYTES) -> None:
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > maximum:
        raise EmailContractError("Normalized email result exceeded its size limit")


def normalize_message(message: Mapping[str, Any], *, provider: str) -> dict[str, Any]:
    if not isinstance(message, Mapping):
        raise EmailContractError("Provider returned an invalid message")
    timestamp = utc_rfc3339(message.get("timestamp"), fallback=message.get("date"))
    return {
        "provider": provider,
        "message_id": str(message.get("message_id") or message.get("id") or ""),
        "conversation_id": str(message.get("conversation_id") or message.get("thread_id") or ""),
        "from": str(message.get("from") or ""),
        "to": _normalized_address_list(message.get("to"), "to"),
        "cc": _normalized_address_list(message.get("cc"), "cc"),
        "bcc": _normalized_address_list(message.get("bcc"), "bcc"),
        "subject": str(message.get("subject") or ""),
        "timestamp": timestamp,
        "snippet": str(message.get("snippet") or ""),
        "text": str(message.get("text") or ""),
        "unread": bool(message.get("unread", False)),
        "attachments": _normalized_attachments(message.get("attachments")),
    }


def _normalized_address_list(value: Any, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EmailContractError(f"Provider returned invalid {field} addresses")
    return [item[:2000] for item in value[:100]]


def _normalized_attachments(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise EmailContractError("Provider returned invalid attachment metadata")
    attachments: list[dict[str, Any]] = []
    for item in value[:100]:
        if not isinstance(item, Mapping):
            raise EmailContractError("Provider returned invalid attachment metadata")
        filename = item.get("filename")
        mime_type = item.get("mime_type")
        size = item.get("size")
        if (
            not isinstance(filename, str)
            or not isinstance(mime_type, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise EmailContractError("Provider returned invalid attachment metadata")
        attachments.append(
            {
                "filename": filename[:1024],
                "mime_type": mime_type[:255],
                "size": size,
            }
        )
    return attachments


def normalize_conversation(summary: Mapping[str, Any], *, provider: str) -> dict[str, Any]:
    if not isinstance(summary, Mapping):
        raise EmailContractError("Provider returned an invalid conversation")
    timestamp = utc_rfc3339(summary.get("latest_timestamp"), fallback=summary.get("latest_date"))
    return {
        "provider": provider,
        "conversation_id": str(summary.get("conversation_id") or summary.get("id") or ""),
        "subject": str(summary.get("subject") or ""),
        "latest_sender": str(summary.get("latest_sender") or ""),
        "latest_timestamp": timestamp,
        "snippet": str(summary.get("snippet") or ""),
        "message_count": int(summary.get("message_count") or 0),
        "unread": bool(summary.get("unread", False)),
        "has_attachment": bool(summary.get("has_attachment", False)),
    }


def normalize_provider_output(output: Mapping[str, Any], *, provider: str) -> dict[str, Any]:
    """Normalize an adapter result while keeping provider code small and bounded."""

    result = dict(output)
    if "conversations" in result:
        if not isinstance(result.get("conversations"), list):
            raise EmailContractError("Provider returned invalid conversation results")
        result["conversations"] = [
            normalize_conversation(item, provider=provider)
            for item in result["conversations"]
        ]
    if "messages" in result:
        if not isinstance(result.get("messages"), list):
            raise EmailContractError("Provider returned invalid message results")
        result["messages"] = [
            normalize_message(item, provider=provider)
            for item in result["messages"]
        ]
    if "provider_errors" in result:
        if not isinstance(result.get("provider_errors"), list):
            raise EmailContractError("Provider returned invalid provider errors")
        normalized_errors: list[dict[str, Any]] = []
        for item in result["provider_errors"]:
            if not isinstance(item, Mapping):
                raise EmailContractError("Provider returned invalid provider error")
            retry = item.get("retry_after_seconds")
            retry_after: int | None = None
            if retry is not None:
                try:
                    retry_after = max(0, min(3600, int(retry)))
                except (TypeError, ValueError, OverflowError):
                    raise EmailContractError("Provider returned invalid retry metadata") from None
            normalized_errors.append(
                provider_error(
                    provider,
                    str(item.get("error_type") or "provider_unavailable"),
                    str(item.get("message") or "Provider failed safely"),
                    retry_after,
                )
            )
        result["provider_errors"] = normalized_errors
    if "mark_outcomes" in result:
        if not isinstance(result.get("mark_outcomes"), list):
            raise EmailContractError("Provider returned invalid mark outcomes")
        normalized_outcomes: list[dict[str, Any]] = []
        for item in result["mark_outcomes"]:
            if not isinstance(item, Mapping):
                raise EmailContractError("Provider returned invalid mark outcome")
            normalized_ids: dict[str, list[str]] = {}
            for field in ("marked_message_ids", "failed_message_ids", "unknown_message_ids"):
                values = item.get(field) or []
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise EmailContractError("Provider returned invalid marked message IDs")
                normalized_ids[field] = values[:50]
            normalized_outcomes.append(
                {
                    "provider": provider,
                    **normalized_ids,
                    "marked_count": len(normalized_ids["marked_message_ids"]),
                    "failed_count": len(normalized_ids["failed_message_ids"]),
                    "unknown_count": len(normalized_ids["unknown_message_ids"]),
                }
            )
        result["mark_outcomes"] = normalized_outcomes
    if "conversation_id" in result:
        result["provider"] = provider
        if "messages" in result:
            result["messages"] = sorted(
                result["messages"],
                key=lambda item: timestamp_key(item["timestamp"]),
            )
    enforce_budget(result)
    return result


__all__ = [
    "COMPOSITE_CURSOR_VERSION",
    "EMAIL_PROVIDERS",
    "EmailContractError",
    "MAX_AGGREGATE_RESULT_BYTES",
    "MAX_CONVERSATION_MESSAGES",
    "MAX_PROVIDER_RESULT_BYTES",
    "MAX_READ_NEW_MESSAGES",
    "MAX_SEARCH_PAGE_SIZE",
    "decode_composite_cursor",
    "encode_composite_cursor",
    "enforce_budget",
    "normalize_conversation",
    "normalize_message",
    "normalize_provider_output",
    "provider_error",
    "timestamp_key",
    "utc_rfc3339",
    "validate_provider",
    "validate_providers",
]
