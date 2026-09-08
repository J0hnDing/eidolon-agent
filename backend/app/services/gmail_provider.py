from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, getaddresses
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.integrations.types import IntegrationOperationSpec
from app.services.email_contracts import timestamp_key, utc_rfc3339
from app.services.github_provider import IntegrationProviderError
from app.services.google_oauth import (
    GoogleOAuthStateStore,
    PendingGoogleOAuth,
    exchange_google_oauth_code,
    google_authorization_url,
    google_identity,
    parse_google_oauth_credential,
    refresh_google_access_token,
)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_OAUTH_SCOPES = ("openid", "email", GMAIL_SCOPE)
GMAIL_OAUTH_REDIRECT_URI = "http://localhost:8000/settings/integrations/gmail/oauth/callback"
GMAIL_OAUTH_RETURN_URL = "http://localhost:5174/settings/integrations"
GMAIL_SECRET_NAMESPACE = "gmail"
MAX_GMAIL_RESULT_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_COUNT = 100
MAX_READ_NEW_COUNT = 50
READ_NEW_QUERY = (
    "is:unread in:inbox category:primary newer_than:1y "
    "-category:promotions -category:social -category:updates -category:forums -in:spam -in:trash"
)

gmail_oauth_state_store = GoogleOAuthStateStore()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


class GmailProviderAdapter(Protocol):
    def authorization_url(self, client_id: str, state: str) -> str: ...

    def exchange_code(self, pending: PendingGoogleOAuth, code: str) -> dict[str, str]: ...

    def identity(self, access_token: str) -> dict[str, str]: ...

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GmailTransportOperation:
    """Provider-private request limits, separate from catalog semantics."""

    operation_id: str
    timeout_seconds: float
    max_provider_response_bytes: int


_TRANSPORT_OPERATIONS = {
    value.operation_id: value
    for value in (
        GmailTransportOperation("email.search", 20, 4_000_000),
        GmailTransportOperation("email.conversation.get", 20, 4_000_000),
        GmailTransportOperation("email.read_new", 20, 4_000_000),
        GmailTransportOperation("email.read_and_mark_new", 30, 4_000_000),
        GmailTransportOperation("email.send", 20, 2_000_000),
    )
}


def _transport_operation(operation: IntegrationOperationSpec) -> GmailTransportOperation:
    try:
        return _TRANSPORT_OPERATIONS[operation.id]
    except KeyError:
        raise IntegrationProviderError("operation_undeclared", "Gmail operation is not implemented") from None


class UrllibGmailProviderAdapter:
    def __init__(self) -> None:
        self._opener = None

    def authorization_url(self, client_id: str, state: str) -> str:
        return google_authorization_url(
            client_id,
            state,
            redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
            scopes=GMAIL_OAUTH_SCOPES,
        )

    def exchange_code(self, pending: PendingGoogleOAuth, code: str) -> dict[str, str]:
        return exchange_google_oauth_code(
            self._request_json,
            pending,
            code,
            redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
            required_scope=GMAIL_SCOPE,
            permission_name="Gmail",
        )

    def identity(self, access_token: str) -> dict[str, str]:
        return google_identity(self._request_json, access_token)

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        operation = _transport_operation(operation)
        bundle = parse_google_oauth_credential(credential)
        access_token = refresh_google_access_token(self._request_json, bundle)
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        if operation.operation_id == "email.search":
            return self._search(operation, input_json, headers)
        if operation.operation_id == "email.conversation.get":
            return self._conversation(operation, input_json, headers)
        if operation.operation_id == "email.read_new":
            return self._read_new(operation, headers)
        if operation.operation_id == "email.read_and_mark_new":
            return self._read_and_mark_new(operation, headers)
        if operation.operation_id == "email.send":
            return self._send(operation, input_json, headers)
        raise IntegrationProviderError("operation_undeclared", "Gmail operation is not implemented")

    def _search(
        self,
        operation: GmailTransportOperation,
        input_json: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        query = self._search_query(input_json)
        params: dict[str, Any] = {"q": query, "maxResults": input_json.get("page_size", 25)}
        if input_json.get("page_token"):
            params["pageToken"] = input_json["page_token"]
        payload = self._request_json(
            f"{GMAIL_API_BASE}/threads?{urlencode(params)}",
            method="GET",
            headers=headers,
            body=None,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        thread_refs = payload.get("threads", [])
        if not isinstance(thread_refs, list) or len(thread_refs) > 25:
            raise IntegrationProviderError("provider_unavailable", "Gmail returned invalid search results")
        conversations = []
        for item in thread_refs:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned invalid search results")
            thread = self._get_thread(operation, item["id"], headers, format_name="full")
            conversations.append(self._conversation_summary(thread))
        next_token = payload.get("nextPageToken")
        if next_token is not None and not isinstance(next_token, str):
            raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid page token")
        return {
            "conversations": conversations,
            "has_more": bool(next_token),
            "next_page_token": next_token,
        }

    def _conversation(
        self,
        operation: GmailTransportOperation,
        input_json: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        conversation_id = self._bounded_string(input_json.get("conversation_id"), "conversation_id", 512)
        thread = self._get_thread(operation, conversation_id, headers, format_name="full")
        result = {
            "provider": "gmail",
            "conversation_id": conversation_id,
            "messages": self._normalized_messages(thread, maximum=MAX_MESSAGE_COUNT),
        }
        self._enforce_result_budget(result)
        return result

    def _read_new(self, operation: GmailTransportOperation, headers: dict[str, str]) -> dict[str, Any]:
        return self._fetch_new(operation, headers, mark_read=False)

    def _read_and_mark_new(self, operation: GmailTransportOperation, headers: dict[str, str]) -> dict[str, Any]:
        return self._fetch_new(operation, headers, mark_read=True)

    def _fetch_new(
        self,
        operation: GmailTransportOperation,
        headers: dict[str, str],
        *,
        mark_read: bool,
    ) -> dict[str, Any]:
        payload = self._request_json(
            f"{GMAIL_API_BASE}/messages?{urlencode({'q': READ_NEW_QUERY, 'maxResults': MAX_READ_NEW_COUNT})}",
            method="GET",
            headers=headers,
            body=None,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        refs = payload.get("messages", [])
        if not isinstance(refs, list) or len(refs) > MAX_READ_NEW_COUNT:
            raise IntegrationProviderError("provider_unavailable", "Gmail returned invalid unread messages")
        messages: list[dict[str, Any]] = []
        message_ids: list[str] = []
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("id"), str):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned invalid unread messages")
            message_id = ref["id"]
            item = self._request_json(
                f"{GMAIL_API_BASE}/messages/{quote(message_id, safe='')}?format=full",
                method="GET",
                headers=headers,
                body=None,
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
            )
            messages.append(self._normalize_message(item))
            message_ids.append(message_id)
        result = {
            "messages": messages,
            "count": len(messages),
            "has_more": bool(payload.get("nextPageToken")),
        }
        self._enforce_result_budget(result)
        if mark_read:
            outcome = {
                "provider": "gmail",
                "marked_message_ids": [],
                "marked_count": 0,
                "failed_message_ids": [],
                "failed_count": 0,
                "unknown_message_ids": [],
                "unknown_count": 0,
            }
            errors: list[dict[str, Any]] = []
            if message_ids:
                try:
                    self._request_bytes(
                        f"{GMAIL_API_BASE}/messages/batchModify",
                        method="POST",
                        headers={**headers, "Content-Type": "application/json"},
                        body=json.dumps({"ids": message_ids, "removeLabelIds": ["UNREAD"]}, separators=(",", ":")).encode(),
                        timeout=operation.timeout_seconds,
                        max_bytes=operation.max_provider_response_bytes,
                    )
                    outcome["marked_message_ids"] = list(message_ids)
                except IntegrationProviderError as exc:
                    self._reconcile_marked_messages(
                        message_ids,
                        headers,
                        operation,
                        outcome,
                        errors,
                    )
                    errors.append({
                        "error_type": exc.error_type,
                        "message": str(exc),
                        "retry_after_seconds": exc.retry_after_seconds,
                    })
            outcome["marked_count"] = len(outcome["marked_message_ids"])
            outcome["failed_count"] = len(outcome["failed_message_ids"])
            outcome["unknown_count"] = len(outcome["unknown_message_ids"])
            result["mark_outcomes"] = [outcome]
            result["provider_errors"] = [
                {
                    "provider": "gmail",
                    "error_type": item["error_type"],
                    "message": item["message"][:500],
                    **(
                        {"retry_after_seconds": item["retry_after_seconds"]}
                        if item.get("retry_after_seconds") is not None
                        else {}
                    ),
                }
                for item in errors
            ]
            if outcome["failed_count"] or outcome["unknown_count"]:
                result["provider_errors"].insert(
                    0,
                    {
                        "provider": "gmail",
                        "error_type": "partial_mutation",
                        "message": "Gmail read state was only partially reconciled",
                    },
                )
            self._enforce_result_budget(result)
        return result

    def _send(
        self,
        operation: GmailTransportOperation,
        input_json: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        recipients = self._send_recipients(input_json)
        subject = self._bounded_string(input_json.get("subject"), "subject", 500)
        body_text = self._bounded_string(input_json.get("body"), "body", 20_000)
        message = EmailMessage(policy=SMTP)
        message["To"] = ", ".join(recipients["to"])
        if recipients["cc"]:
            message["Cc"] = ", ".join(recipients["cc"])
        if recipients["bcc"]:
            message["Bcc"] = ", ".join(recipients["bcc"])
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message.set_content(body_text)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        payload = self._request_json(
            f"{GMAIL_API_BASE}/messages/send",
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
            body=json.dumps({"raw": raw}, separators=(",", ":")).encode(),
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        message_id = self._bounded_string(payload.get("id"), "message id", 512, provider_response=True)
        conversation_id = self._bounded_string(payload.get("threadId"), "conversation id", 512, provider_response=True)
        return {
            "provider": "gmail",
            "sent": True,
            "message_id": message_id,
            "conversation_id": conversation_id,
        }

    def _get_thread(
        self,
        operation: GmailTransportOperation,
        conversation_id: str,
        headers: dict[str, str],
        *,
        format_name: str,
    ) -> dict[str, Any]:
        params: list[tuple[str, str]] = [("format", format_name)]
        if format_name == "metadata":
            params.extend(("metadataHeaders", name) for name in ("Subject", "From", "Date"))
        return self._request_json(
            f"{GMAIL_API_BASE}/threads/{quote(conversation_id, safe='')}?{urlencode(params)}",
            method="GET",
            headers=headers,
            body=None,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )

    def _conversation_summary(self, thread: dict[str, Any]) -> dict[str, Any]:
        conversation_id = self._bounded_string(thread.get("id"), "conversation id", 512, provider_response=True)
        messages = thread.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 10_000:
            raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid conversation")
        candidates: list[tuple[dict[str, Any], dict[str, str], str]] = []
        for message in messages:
            if not isinstance(message, dict):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid conversation")
            headers = self._headers(message)
            candidates.append((message, headers, self._message_timestamp(message, headers)))
        latest, headers, latest_timestamp = max(candidates, key=lambda item: item[2])
        snippet = self._bounded_optional_string(latest.get("snippet"), "snippet", 2_000)
        return {
            "provider": "gmail",
            "conversation_id": conversation_id,
            "subject": headers.get("subject", ""),
            "latest_sender": headers.get("from", ""),
            "latest_timestamp": latest_timestamp,
            "snippet": snippet or "",
            "message_count": len(messages),
            "unread": any(self._has_label(item, "UNREAD") for item in messages),
            "has_attachment": any(self._attachment_metadata(item) for item in messages),
        }

    def _normalized_messages(self, thread: dict[str, Any], *, maximum: int) -> list[dict[str, Any]]:
        messages = thread.get("messages")
        if not isinstance(messages, list) or len(messages) > 10_000:
            raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid conversation")
        normalized = [self._normalize_message(item) for item in messages[:maximum]]
        normalized.sort(key=lambda item: timestamp_key(item["timestamp"]))
        return normalized

    def _reconcile_marked_messages(
        self,
        message_ids: list[str],
        headers: dict[str, str],
        operation: GmailTransportOperation,
        outcome: dict[str, Any],
        errors: list[dict[str, Any]],
    ) -> None:
        for message_id in message_ids:
            try:
                state = self._request_json(
                    f"{GMAIL_API_BASE}/messages/{quote(message_id, safe='')}?format=minimal",
                    method="GET",
                    headers=headers,
                    body=None,
                    timeout=operation.timeout_seconds,
                    max_bytes=operation.max_provider_response_bytes,
                )
                labels = state.get("labelIds")
                if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
                    outcome["unknown_message_ids"].append(message_id)
                elif "UNREAD" in labels:
                    outcome["failed_message_ids"].append(message_id)
                else:
                    outcome["marked_message_ids"].append(message_id)
            except IntegrationProviderError as exc:
                outcome["unknown_message_ids"].append(message_id)
                errors.append({
                    "error_type": exc.error_type,
                    "message": str(exc),
                    "retry_after_seconds": exc.retry_after_seconds,
                })

    def _normalize_message(self, message: Any) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid message")
        message_id = self._bounded_string(message.get("id"), "message id", 512, provider_response=True)
        conversation_id = self._bounded_string(message.get("threadId"), "conversation id", 512, provider_response=True)
        headers = self._headers(message)
        plain_parts, html_parts = self._body_parts(message.get("payload"))
        text = "\n\n".join(part for part in plain_parts if part)
        if not text and html_parts:
            parser = _TextHTMLParser()
            for html in html_parts:
                parser.feed(html)
            text = parser.text()
        return {
            "provider": "gmail",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "from": headers.get("from", ""),
            "to": self._address_values(headers.get("to", "")),
            "cc": self._address_values(headers.get("cc", "")),
            "bcc": self._address_values(headers.get("bcc", "")),
            "subject": headers.get("subject", ""),
            "timestamp": self._message_timestamp(message, headers),
            "snippet": self._bounded_optional_string(message.get("snippet"), "snippet", 2_000) or "",
            "text": text,
            "unread": self._has_label(message, "UNREAD"),
            "attachments": self._attachment_metadata(message),
        }

    def _body_parts(self, payload: Any) -> tuple[list[str], list[str]]:
        plain: list[str] = []
        html: list[str] = []

        def visit(part: Any) -> None:
            if not isinstance(part, dict):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid MIME structure")
            filename = part.get("filename")
            if filename not in {None, ""}:
                return
            mime_type = part.get("mimeType")
            body = part.get("body", {})
            if not isinstance(body, dict):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid MIME body")
            data = body.get("data")
            if isinstance(data, str) and mime_type in {"text/plain", "text/html"}:
                decoded = self._decode_body(data)
                (plain if mime_type == "text/plain" else html).append(decoded)
            children = part.get("parts", [])
            if not isinstance(children, list):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid MIME structure")
            for child in children:
                visit(child)

        visit(payload)
        return plain, html

    def _attachment_metadata(self, message: Any) -> list[dict[str, Any]]:
        if not isinstance(message, dict):
            raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid message")
        attachments: list[dict[str, Any]] = []

        def visit(part: Any) -> None:
            if not isinstance(part, dict):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid MIME structure")
            filename = part.get("filename")
            body = part.get("body", {})
            if not isinstance(body, dict):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid MIME body")
            if isinstance(filename, str) and filename:
                size = body.get("size", 0)
                if not isinstance(size, int) or size < 0:
                    raise IntegrationProviderError("provider_unavailable", "Gmail returned invalid attachment metadata")
                attachments.append(
                    {
                        "filename": self._bounded_string(filename, "attachment filename", 1_024, provider_response=True),
                        "mime_type": self._bounded_string(
                            part.get("mimeType", "application/octet-stream"),
                            "attachment MIME type",
                            255,
                            provider_response=True,
                        ),
                        "size": size,
                    }
                )
                if len(attachments) > 100:
                    raise IntegrationProviderError("response_too_large", "Gmail returned too many attachments")
            children = part.get("parts", [])
            if not isinstance(children, list):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned an invalid MIME structure")
            for child in children:
                visit(child)

        visit(message.get("payload"))
        return attachments

    def _headers(self, message: dict[str, Any]) -> dict[str, str]:
        payload = message.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("headers", []), list):
            raise IntegrationProviderError("provider_unavailable", "Gmail returned invalid message headers")
        result: dict[str, str] = {}
        limits = {"from": 2_000, "to": 10_000, "cc": 10_000, "bcc": 10_000, "subject": 2_000, "date": 128}
        for item in payload.get("headers", []):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
                raise IntegrationProviderError("provider_unavailable", "Gmail returned invalid message headers")
            name = item["name"].lower()
            if name in {"from", "to", "cc", "bcc", "subject", "date"}:
                result[name] = self._bounded_string(
                    item["value"], f"{name} header", limits[name], provider_response=True
                )
        return result

    @staticmethod
    def _message_timestamp(message: dict[str, Any], headers: dict[str, str]) -> str:
        """Prefer Gmail's millisecond internal timestamp, with Date fallback."""

        return utc_rfc3339(message.get("internalDate"), fallback=headers.get("date"))

    @staticmethod
    def _has_label(message: Any, label: str) -> bool:
        return isinstance(message, dict) and isinstance(message.get("labelIds"), list) and label in message["labelIds"]

    @staticmethod
    def _address_values(value: str) -> list[str]:
        return [address for _name, address in getaddresses([value]) if address][:100]

    def _search_query(self, input_json: dict[str, Any]) -> str:
        parts: list[str] = []
        keywords = input_json.get("keywords")
        if keywords:
            parts.append(self._gmail_quote(self._bounded_string(keywords, "keywords", 500)))
        for field in ("from", "to", "subject"):
            value = input_json.get(field)
            if value:
                parts.append(f"{field}:{self._gmail_quote(self._bounded_string(value, field, 500))}")
        for field in ("after", "before"):
            value = input_json.get(field)
            if value:
                value = self._bounded_string(value, field, 10)
                try:
                    parsed = date.fromisoformat(value)
                except ValueError:
                    raise IntegrationProviderError("invalid_input", f"{field} must be YYYY-MM-DD") from None
                parts.append(f"{field}:{parsed:%Y/%m/%d}")
        if "unread" in input_json:
            parts.append("is:unread" if input_json["unread"] else "-is:unread")
        if "has_attachment" in input_json:
            parts.append("has:attachment" if input_json["has_attachment"] else "-has:attachment")
        if not parts:
            raise IntegrationProviderError("invalid_input", "At least one email search filter is required")
        page_size = input_json.get("page_size", 25)
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 25:
            raise IntegrationProviderError("invalid_input", "page_size must be from 1 to 25")
        return " ".join(parts)

    def _send_recipients(self, input_json: dict[str, Any]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for field in ("to", "cc", "bcc"):
            value = input_json.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise IntegrationProviderError("invalid_input", f"{field} must contain email addresses")
            parsed = [address for _name, address in getaddresses(value) if address]
            if len(parsed) != len(value) or any(
                re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address) is None for address in parsed
            ):
                raise IntegrationProviderError("invalid_input", f"{field} contains an invalid email address")
            result[field] = parsed
        if not 1 <= len(result["to"]) <= 10:
            raise IntegrationProviderError("invalid_input", "to must contain 1 to 10 email addresses")
        if sum(len(value) for value in result.values()) > 20:
            raise IntegrationProviderError("invalid_input", "Email may have at most 20 total recipients")
        return result

    @staticmethod
    def _gmail_quote(value: str) -> str:
        return f'"{value.replace("\\", "\\\\").replace(chr(34), chr(92) + chr(34))}"'

    @staticmethod
    def _decode_body(data: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
            return raw.decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            raise IntegrationProviderError("provider_unavailable", "Gmail returned invalid message content") from None

    @staticmethod
    def _bounded_string(value: Any, field: str, maximum: int, *, provider_response: bool = False) -> str:
        error_type = "provider_unavailable" if provider_response else "invalid_input"
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise IntegrationProviderError(error_type, f"{field} is invalid")
        return value

    @staticmethod
    def _bounded_optional_string(value: Any, field: str, maximum: int) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > maximum:
            raise IntegrationProviderError("provider_unavailable", f"Gmail returned an invalid {field}")
        return value

    @staticmethod
    def _enforce_result_budget(result: dict[str, Any]) -> None:
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_GMAIL_RESULT_BYTES:
            raise IntegrationProviderError("response_too_large", "Gmail result exceeded the operation limit")

    def _request_json(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
        max_bytes: int,
        oauth_request: bool = False,
    ) -> dict[str, Any]:
        raw = self._request_bytes(
            url,
            method=method,
            headers=headers,
            body=body,
            timeout=timeout,
            max_bytes=max_bytes,
            oauth_request=oauth_request,
        )
        try:
            payload = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise IntegrationProviderError("provider_unavailable", "Google returned an invalid response") from None
        if not isinstance(payload, dict):
            raise IntegrationProviderError("provider_unavailable", "Google returned an invalid response")
        return payload

    def _request_bytes(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
        max_bytes: int,
        oauth_request: bool = False,
    ) -> bytes:
        request = Request(url, data=body, method=method, headers=headers)
        try:
            if self._opener is None:
                self._opener = build_opener(_NoRedirect())
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(max_bytes + 1)
        except HTTPError as exc:
            raise self._http_error(exc, oauth_request=oauth_request) from None
        except (TimeoutError, URLError):
            raise IntegrationProviderError("provider_timeout", "Google did not respond before the timeout") from None
        if len(raw) > max_bytes:
            raise IntegrationProviderError("response_too_large", "Google response exceeded the operation limit")
        return raw

    def _http_error(self, exc: HTTPError, *, oauth_request: bool) -> IntegrationProviderError:
        retry_after = self._retry_after(exc.headers.get("Retry-After"))
        if oauth_request and exc.code == 400:
            error_type = "invalid_credential"
        elif exc.code == 400:
            error_type = "invalid_input"
        elif exc.code == 401:
            error_type = "invalid_credential"
        elif exc.code in {404, 410}:
            error_type = "not_found"
        elif exc.code == 429:
            error_type = "rate_limited"
        elif exc.code == 403:
            error_type = "provider_forbidden"
        elif 500 <= exc.code <= 599:
            error_type = "provider_unavailable"
        else:
            error_type = "provider_unavailable"
        return IntegrationProviderError(error_type, "Google request failed", retry_after_seconds=retry_after)

    @staticmethod
    def _retry_after(value: str | None) -> int | None:
        try:
            parsed = int(value or "")
        except ValueError:
            return None
        return parsed if 0 <= parsed <= 3600 else None


class FakeGmailProviderAdapter:
    def __init__(self, *, email: str = "person@example.com", account_id: str = "gmail-account") -> None:
        self.email = email
        self.account_id = account_id
        self.error_type: str | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.conversations: dict[str, list[dict[str, Any]]] = {}

    def authorization_url(self, client_id: str, state: str) -> str:
        return google_authorization_url(
            client_id,
            state,
            redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
            scopes=GMAIL_OAUTH_SCOPES,
        )

    def exchange_code(self, pending: PendingGoogleOAuth, code: str) -> dict[str, str]:
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Gmail OAuth failure")
        return {"access_token": f"access-{code}", "refresh_token": "fake-refresh-token"}

    def identity(self, access_token: str) -> dict[str, str]:
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Gmail identity failure")
        return {"account_id": self.account_id, "email": self.email}

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        operation = _transport_operation(operation)
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Gmail failure")
        self.calls.append((operation.operation_id, input_json))
        if operation.operation_id == "email.search":
            summaries = []
            for conversation_id, messages in self.conversations.items():
                latest = messages[-1]
                summaries.append(
                    {
                        "provider": "gmail",
                        "conversation_id": conversation_id,
                        "subject": latest.get("subject", ""),
                        "latest_sender": latest.get("from", ""),
                        "latest_timestamp": utc_rfc3339(latest.get("timestamp"), fallback=latest.get("date")),
                        "snippet": latest.get("snippet", ""),
                        "message_count": len(messages),
                        "unread": any(item.get("unread", False) for item in messages),
                        "has_attachment": any(item.get("attachments") for item in messages),
                    }
                )
            return {"conversations": summaries, "has_more": False, "next_page_token": None}
        if operation.operation_id == "email.conversation.get":
            conversation_id = input_json["conversation_id"]
            if conversation_id not in self.conversations:
                raise IntegrationProviderError("not_found", "Fake conversation was not found")
            return {
                "provider": "gmail",
                "conversation_id": conversation_id,
                "messages": [
                    {**item, "provider": "gmail", "timestamp": utc_rfc3339(item.get("timestamp"), fallback=item.get("date"))}
                    for item in self.conversations[conversation_id][:100]
                ],
            }
        if operation.operation_id in {"email.read_new", "email.read_and_mark_new"}:
            messages = [
                item
                for conversation in self.conversations.values()
                for item in conversation
                if item.get("unread", False)
            ][:MAX_READ_NEW_COUNT]
            returned = [dict(item) for item in messages]
            if operation.operation_id == "email.read_and_mark_new":
                for item in messages:
                    item["unread"] = False
            return {
                "messages": [
                    {**item, "provider": "gmail", "timestamp": utc_rfc3339(item.get("timestamp"), fallback=item.get("date"))}
                    for item in returned
                ],
                "count": len(returned),
                "has_more": False,
                **(
                    {
                        "mark_outcomes": [{
                            "provider": "gmail",
                            "marked_message_ids": [item.get("message_id", "") for item in returned],
                            "marked_count": len(returned),
                            "failed_message_ids": [],
                            "failed_count": 0,
                            "unknown_message_ids": [],
                            "unknown_count": 0,
                        }],
                        "provider_errors": [],
                    }
                    if operation.operation_id == "email.read_and_mark_new"
                    else {}
                ),
            }
        if operation.operation_id == "email.send":
            index = sum(len(messages) for messages in self.conversations.values()) + 1
            return {
                "provider": "gmail",
                "sent": True,
                "message_id": f"message-{index}",
                "conversation_id": f"thread-{index}",
            }
        raise IntegrationProviderError("operation_undeclared", "Fake operation is not implemented")
