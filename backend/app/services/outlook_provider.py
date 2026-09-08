"""Microsoft Graph Outlook adapter and OAuth helpers.

Only this module knows Graph URLs, query syntax, immutable-id headers, and
batch response details. The integration registry exposes the shared email
contract instead.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import getaddresses
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.integrations.types import IntegrationOperationSpec
from app.services.email_contracts import (
    MAX_CONVERSATION_MESSAGES,
    MAX_PROVIDER_RESULT_BYTES,
    MAX_READ_NEW_MESSAGES,
    enforce_budget,
    utc_rfc3339,
)
from app.services.github_provider import IntegrationProviderError

OUTLOOK_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
OUTLOOK_AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
OUTLOOK_AUTHORIZATION_URL = f"{OUTLOOK_AUTHORITY}/authorize"
OUTLOOK_TOKEN_URL = f"{OUTLOOK_AUTHORITY}/token"
OUTLOOK_SCOPE = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
)
OUTLOOK_OAUTH_REDIRECT_URI = "http://localhost:8000/settings/integrations/outlook/oauth/callback"
OUTLOOK_OAUTH_RETURN_URL = "http://localhost:5174/settings/integrations"
OUTLOOK_CLIENT_SECRET_NAMESPACE = "microsoft_oauth"
OUTLOOK_SECRET_NAMESPACE = "outlook"
MAX_GRAPH_RESPONSE_BYTES = 4_000_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


@dataclass(frozen=True)
class PendingOutlookOAuth:
    client_id: str
    client_secret: str
    code_verifier: str
    expires_at: float


class OutlookOAuthStateStore:
    def __init__(self, *, ttl_seconds: int = 600, max_pending: int = 8) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_pending = max_pending
        self._pending: dict[str, PendingOutlookOAuth] = {}
        self._lock = threading.Lock()

    def create(self, client_id: str, client_secret: str) -> tuple[str, str]:
        now = time.monotonic()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(32)
        with self._lock:
            self._prune(now)
            while len(self._pending) >= self.max_pending:
                oldest = min(self._pending, key=lambda key: self._pending[key].expires_at)
                self._pending.pop(oldest, None)
            self._pending[state] = PendingOutlookOAuth(
                client_id=client_id,
                client_secret=client_secret,
                code_verifier=verifier,
                expires_at=now + self.ttl_seconds,
            )
        return state, challenge

    def consume(self, state: str) -> PendingOutlookOAuth:
        now = time.monotonic()
        with self._lock:
            pending = self._pending.pop(state, None)
            self._prune(now)
        if pending is None or pending.expires_at <= now:
            raise IntegrationProviderError("invalid_credential", "Microsoft OAuth state is invalid or expired")
        return pending

    def _prune(self, now: float) -> None:
        for state in [key for key, item in self._pending.items() if item.expires_at <= now]:
            self._pending.pop(state, None)


outlook_oauth_state_store = OutlookOAuthStateStore()


def outlook_authorization_url(client_id: str, state: str, code_challenge: str) -> str:
    return f"{OUTLOOK_AUTHORIZATION_URL}?{urlencode({
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': OUTLOOK_OAUTH_REDIRECT_URI,
        'response_mode': 'query',
        'scope': ' '.join(OUTLOOK_SCOPE),
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        'prompt': 'select_account',
    })}"


def _parse_token_payload(payload: dict[str, Any]) -> dict[str, str]:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token or not isinstance(refresh_token, str) or not refresh_token:
        raise IntegrationProviderError("invalid_credential", "Microsoft OAuth did not return required tokens")
    granted = payload.get("scope")
    granted_scopes = set(granted.split()) if isinstance(granted, str) else set()
    missing = {"Mail.ReadWrite", "Mail.Send"} - granted_scopes
    if missing:
        raise IntegrationProviderError("provider_forbidden", "Microsoft mail permission was not granted")
    return {"access_token": access_token, "refresh_token": refresh_token}


def serialize_outlook_credential(refresh_token: str) -> str:
    if not isinstance(refresh_token, str) or not 1 <= len(refresh_token) <= 16_384:
        raise IntegrationProviderError("invalid_credential", "Stored Outlook authorization is invalid")
    return json.dumps({"refresh_token": refresh_token}, separators=(",", ":"), sort_keys=True)


def parse_outlook_credential(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise IntegrationProviderError("invalid_credential", "Stored Outlook authorization is invalid") from None
    allowed = {"refresh_token", "client_id", "client_secret"}
    if (
        not isinstance(parsed, dict)
        or not set(parsed).issubset(allowed)
        or not isinstance(parsed.get("refresh_token"), str)
        or not parsed["refresh_token"]
    ):
        raise IntegrationProviderError("invalid_credential", "Stored Outlook authorization is invalid")
    return parsed


def _safe_identity(payload: dict[str, Any]) -> dict[str, str]:
    subject = payload.get("id")
    email = payload.get("mail") or payload.get("userPrincipalName")
    if not isinstance(subject, str) or not subject or not isinstance(email, str) or not email:
        raise IntegrationProviderError("provider_unavailable", "Microsoft returned an invalid account identity")
    return {
        "account_id": hashlib.sha256(f"microsoft:{subject}".encode()).hexdigest(),
        "email": " ".join(email.split())[:320],
    }


class OutlookTransport(Protocol):
    def request_json(self, url: str, *, method: str, headers: dict[str, str], body: bytes | None, timeout: float, max_bytes: int, oauth_request: bool = False) -> dict[str, Any]: ...

    def request_bytes(self, url: str, *, method: str, headers: dict[str, str], body: bytes | None, timeout: float, max_bytes: int, oauth_request: bool = False) -> bytes: ...


class UrllibOutlookTransport:
    def __init__(self) -> None:
        self._opener = None

    def request_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        raw = self.request_bytes(url, **kwargs)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise IntegrationProviderError("provider_unavailable", "Microsoft returned an invalid response") from None
        if not isinstance(payload, dict):
            raise IntegrationProviderError("provider_unavailable", "Microsoft returned an invalid response")
        return payload

    def request_bytes(
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
            retry_after = _retry_after(exc.headers.get("Retry-After"))
            if oauth_request and exc.code in {400, 401}:
                error_type = "invalid_credential"
            elif exc.code == 400:
                error_type = "invalid_input"
            elif exc.code == 401:
                error_type = "invalid_credential"
            elif exc.code == 403:
                error_type = "provider_forbidden"
            elif exc.code == 404:
                error_type = "not_found"
            elif exc.code == 429:
                error_type = "rate_limited"
            elif 500 <= exc.code <= 599:
                error_type = "provider_unavailable"
            else:
                error_type = "provider_unavailable"
            raise IntegrationProviderError(error_type, "Microsoft Graph request failed", retry_after_seconds=retry_after) from None
        except (TimeoutError, URLError):
            raise IntegrationProviderError("provider_timeout", "Microsoft Graph did not respond before the timeout") from None
        if len(raw) > max_bytes:
            raise IntegrationProviderError("response_too_large", "Microsoft Graph response exceeded the operation limit")
        return raw


def _retry_after(value: str | None) -> int | None:
    try:
        parsed = int(value or "")
    except ValueError:
        return None
    return parsed if 0 <= parsed <= 3600 else None


def _safe_graph_cursor(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise IntegrationProviderError("invalid_input", "Outlook continuation token is invalid")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith("/") or "\\" in value:
        raise IntegrationProviderError("invalid_input", "Outlook continuation token is invalid")
    if any(ord(char) < 0x20 for char in value):
        raise IntegrationProviderError("invalid_input", "Outlook continuation token is invalid")
    return value


def _next_skip_token(payload: dict[str, Any]) -> str | None:
    next_link = payload.get("@odata.nextLink")
    if next_link is None:
        return None
    if not isinstance(next_link, str):
        raise IntegrationProviderError("provider_unavailable", "Microsoft returned an invalid continuation link")
    parsed = urlsplit(next_link)
    if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or parsed.path != "/v1.0/me/messages":
        raise IntegrationProviderError("provider_unavailable", "Microsoft returned an unexpected continuation link")
    values = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return _safe_graph_cursor(values.get("$skiptoken"))


class OutlookProviderAdapter:
    provider_id = "outlook"

    def __init__(self, transport: OutlookTransport | None = None) -> None:
        self.transport = transport or UrllibOutlookTransport()

    def authorization_url(self, client_id: str, client_secret: str) -> str:
        state, challenge = outlook_oauth_state_store.create(client_id, client_secret)
        return outlook_authorization_url(client_id, state, challenge)

    def begin_authorization(
        self,
        client_id: str,
        client_secret: str,
        state_store: OutlookOAuthStateStore | None = None,
    ) -> tuple[str, str]:
        state, challenge = (state_store or outlook_oauth_state_store).create(client_id, client_secret)
        return state, outlook_authorization_url(client_id, state, challenge)

    def exchange_code(self, pending: PendingOutlookOAuth, code: str) -> dict[str, str]:
        if not isinstance(code, str) or not code:
            raise IntegrationProviderError("invalid_input", "Microsoft OAuth code is required")
        payload = self.transport.request_json(
            OUTLOOK_TOKEN_URL,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode({
                "client_id": pending.client_id,
                "client_secret": pending.client_secret,
                "code": code,
                "code_verifier": pending.code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": OUTLOOK_OAUTH_REDIRECT_URI,
            }).encode(),
            timeout=15,
            max_bytes=1_000_000,
            oauth_request=True,
        )
        return _parse_token_payload(payload)

    def identity(self, access_token: str) -> dict[str, str]:
        payload = self.transport.request_json(
            f"{OUTLOOK_GRAPH_BASE}/me?$select=id,mail,userPrincipalName",
            method="GET",
            headers=self._headers(access_token),
            body=None,
            timeout=15,
            max_bytes=1_000_000,
        )
        return _safe_identity(payload)

    def execute(self, operation: IntegrationOperationSpec, input_json: dict[str, Any], credential: str) -> dict[str, Any]:
        bundle = parse_outlook_credential(credential)
        access_token = self._refresh(bundle)
        if operation.id == "email.search":
            return self._search(input_json, access_token)
        if operation.id == "email.conversation.get":
            return self._conversation(input_json, access_token)
        if operation.id == "email.read_new":
            return self._read_new(access_token)
        if operation.id == "email.read_and_mark_new":
            return self._read_and_mark_new(access_token)
        if operation.id == "email.send":
            return self._send(input_json, access_token)
        raise IntegrationProviderError("operation_undeclared", "Outlook operation is not implemented")

    def _refresh(self, bundle: dict[str, str]) -> str:
        payload = self.transport.request_json(
            OUTLOOK_TOKEN_URL,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode({
                "client_id": bundle.get("client_id", ""),
                "client_secret": bundle.get("client_secret", ""),
                "refresh_token": bundle["refresh_token"],
                "grant_type": "refresh_token",
                "scope": " ".join(OUTLOOK_SCOPE),
            }).encode(),
            timeout=15,
            max_bytes=1_000_000,
            oauth_request=True,
        )
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise IntegrationProviderError("invalid_credential", "Microsoft access-token refresh failed")
        return access_token

    @staticmethod
    def _headers(access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"',
        }

    def _request(self, url: str, *, method: str, access_token: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.transport.request_json(
            url,
            method=method,
            headers={**self._headers(access_token), **({"Content-Type": "application/json"} if body is not None else {})},
            body=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            timeout=20,
            max_bytes=MAX_GRAPH_RESPONSE_BYTES,
        )

    def _search(self, input_json: dict[str, Any], access_token: str) -> dict[str, Any]:
        page_size = input_json.get("page_size", 25)
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 25:
            raise IntegrationProviderError("invalid_input", "page_size must be from 1 to 25")
        params: list[tuple[str, str]] = [
            ("$top", str(page_size)),
            ("$select", "id,conversationId,subject,from,receivedDateTime,sentDateTime,bodyPreview,isRead,hasAttachments,attachments"),
        ]
        search = self._graph_search_expression(input_json)
        if search:
            params.append(("$search", f'"{search}"'))
        filters: list[str] = []
        for name, operator in (("after", "ge"), ("before", "lt")):
            value = input_json.get(name)
            if value:
                try:
                    parsed = date.fromisoformat(str(value))
                except ValueError:
                    raise IntegrationProviderError("invalid_input", f"{name} must be YYYY-MM-DD") from None
                if name == "before":
                    parsed = parsed + timedelta(days=1)
                filters.append(f"receivedDateTime {operator} {parsed.isoformat()}T00:00:00Z")
        if "unread" in input_json:
            filters.append(f"isRead eq {str(not bool(input_json['unread'])).lower()}")
        if "has_attachment" in input_json:
            filters.append(f"hasAttachments eq {str(bool(input_json['has_attachment'])).lower()}")
        if filters:
            params.append(("$filter", " and ".join(filters)))
        if not search and not filters:
            raise IntegrationProviderError("invalid_input", "At least one email search filter is required")
        cursor = _safe_graph_cursor(input_json.get("page_token"))
        if cursor is not None:
            params.append(("$skiptoken", cursor))
        payload = self._request(f"{OUTLOOK_GRAPH_BASE}/me/messages?{urlencode(params)}", method="GET", access_token=access_token)
        messages = payload.get("value")
        if not isinstance(messages, list) or len(messages) > 25:
            raise IntegrationProviderError("provider_unavailable", "Microsoft returned invalid search results")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for message in messages:
            if not isinstance(message, dict):
                raise IntegrationProviderError("provider_unavailable", "Microsoft returned invalid search results")
            conversation_id = _bounded(message.get("conversationId"), "conversation id", provider=True)
            grouped.setdefault(conversation_id, []).append(message)
        summaries = [self._conversation_summary(conversation_id, values) for conversation_id, values in grouped.items()]
        summaries.sort(key=lambda item: item["latest_timestamp"], reverse=True)
        result = {
            "conversations": summaries,
            "has_more": _next_skip_token(payload) is not None,
            "next_page_token": _next_skip_token(payload),
        }
        enforce_budget(result, maximum=MAX_PROVIDER_RESULT_BYTES)
        return result

    def _graph_search_expression(self, input_json: dict[str, Any]) -> str:
        fields: list[str] = []
        keywords = input_json.get("keywords")
        if keywords:
            fields.append(self._escape_search_value(str(keywords), "keywords"))
        for name in ("from", "to", "subject"):
            value = input_json.get(name)
            if value:
                fields.append(f"{name}:{self._escape_search_value(str(value), name)}")
        return " AND ".join(fields)

    @staticmethod
    def _escape_search_value(value: str, field: str) -> str:
        if not value or len(value) > 500 or any(ord(char) < 0x20 for char in value):
            raise IntegrationProviderError("invalid_input", f"{field} is invalid")
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _conversation(self, input_json: dict[str, Any], access_token: str) -> dict[str, Any]:
        conversation_id = _bounded(input_json.get("conversation_id"), "conversation id")
        filter_value = conversation_id.replace("'", "''")
        params = urlencode({
            "$filter": f"conversationId eq '{filter_value}'",
            "$top": str(MAX_CONVERSATION_MESSAGES),
            "$orderby": "receivedDateTime asc",
            "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,bccRecipients,receivedDateTime,sentDateTime,body,bodyPreview,isRead,hasAttachments,attachments",
            "$expand": "attachments($select=name,contentType,size)",
        })
        payload = self._request(f"{OUTLOOK_GRAPH_BASE}/me/messages?{params}", method="GET", access_token=access_token)
        messages = payload.get("value")
        if not isinstance(messages, list) or len(messages) > MAX_CONVERSATION_MESSAGES:
            raise IntegrationProviderError("provider_unavailable", "Microsoft returned an invalid conversation")
        normalized = [self._normalize_message(item) for item in messages]
        normalized.sort(key=lambda item: item["timestamp"])
        result = {"provider": "outlook", "conversation_id": conversation_id, "messages": normalized}
        enforce_budget(result)
        return result

    def _read_new(self, access_token: str) -> dict[str, Any]:
        since = (datetime.now(UTC) - timedelta(days=365)).isoformat().replace("+00:00", "Z")
        params = urlencode({
            "$filter": f"isRead eq false and inferenceClassification eq 'focused' and receivedDateTime ge {since}",
            "$top": str(MAX_READ_NEW_MESSAGES),
            "$orderby": "receivedDateTime desc",
            "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,bccRecipients,receivedDateTime,sentDateTime,body,bodyPreview,isRead,hasAttachments,attachments",
            "$expand": "attachments($select=name,contentType,size)",
        })
        payload = self._request(f"{OUTLOOK_GRAPH_BASE}/me/mailFolders/inbox/messages?{params}", method="GET", access_token=access_token)
        messages = payload.get("value")
        if not isinstance(messages, list) or len(messages) > MAX_READ_NEW_MESSAGES:
            raise IntegrationProviderError("provider_unavailable", "Microsoft returned invalid unread messages")
        normalized = [self._normalize_message(item) for item in messages]
        result = {"messages": normalized, "count": len(normalized), "has_more": False}
        enforce_budget(result)
        return result

    def _read_and_mark_new(self, access_token: str) -> dict[str, Any]:
        result = self._read_new(access_token)
        message_ids = [item["message_id"] for item in result["messages"]]
        outcome = {
            "provider": "outlook",
            "marked_message_ids": [],
            "marked_count": 0,
            "failed_message_ids": [],
            "failed_count": 0,
            "unknown_message_ids": [],
            "unknown_count": 0,
        }
        errors: list[dict[str, Any]] = []
        for start in range(0, len(message_ids), 20):
            batch = message_ids[start:start + 20]
            try:
                payload = self._request(
                    f"{OUTLOOK_GRAPH_BASE}/$batch",
                    method="POST",
                    access_token=access_token,
                    body={"requests": [
                        {
                            "id": str(index),
                            "method": "PATCH",
                            "url": f"/me/messages/{quote(message_id, safe='')}?%24select=id,isRead",
                            "headers": {
                                "Content-Type": "application/json",
                                "Prefer": 'IdType="ImmutableId"',
                            },
                            "body": {"isRead": True},
                        }
                        for index, message_id in enumerate(batch, start=1)
                    ]},
                )
            except IntegrationProviderError as exc:
                self._reconcile_unknown_marks(batch, outcome, errors, access_token)
                errors.append({"error_type": exc.error_type, "message": str(exc), "retry_after_seconds": exc.retry_after_seconds})
                continue
            responses = payload.get("responses")
            response_map: dict[str, dict[str, Any]] = {}
            malformed_response = not isinstance(responses, list)
            if isinstance(responses, list):
                for item in responses:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), (str, int)):
                        malformed_response = True
                        continue
                    response_id = str(item["id"])
                    if response_id in response_map:
                        malformed_response = True
                        continue
                    response_map[response_id] = item
            if malformed_response:
                errors.append({
                    "error_type": "provider_unavailable",
                    "message": "Microsoft returned an invalid batch response",
                    "retry_after_seconds": None,
                })
            ambiguous: list[str] = []
            for index, message_id in enumerate(batch, start=1):
                response = response_map.get(str(index))
                status = response.get("status") if isinstance(response, dict) else None
                if isinstance(status, int) and 200 <= status < 300:
                    outcome["marked_message_ids"].append(message_id)
                elif isinstance(status, int) and status < 500:
                    outcome["failed_message_ids"].append(message_id)
                else:
                    ambiguous.append(message_id)
            if ambiguous:
                self._reconcile_unknown_marks(ambiguous, outcome, errors, access_token)
        outcome["marked_count"] = len(outcome["marked_message_ids"])
        outcome["failed_count"] = len(outcome["failed_message_ids"])
        outcome["unknown_count"] = len(outcome["unknown_message_ids"])
        if outcome["failed_count"] or outcome["unknown_count"]:
            errors.insert(0, {"error_type": "partial_mutation", "message": "Outlook read state was only partially reconciled", "retry_after_seconds": None})
        result["mark_outcomes"] = [outcome]
        result["provider_errors"] = [
            {
                "provider": "outlook",
                "error_type": item["error_type"],
                "message": item["message"][:500],
                **({"retry_after_seconds": item["retry_after_seconds"]} if item.get("retry_after_seconds") is not None else {}),
            }
            for item in errors
        ]
        enforce_budget(result)
        return result

    def _reconcile_unknown_marks(
        self,
        message_ids: list[str],
        outcome: dict[str, Any],
        errors: list[dict[str, Any]],
        access_token: str,
    ) -> None:
        for message_id in message_ids:
            try:
                state = self._request(
                    f"{OUTLOOK_GRAPH_BASE}/me/messages/{quote(message_id, safe='')}?%24select=id,isRead",
                    method="GET",
                    access_token=access_token,
                )
                if state.get("isRead") is True:
                    outcome["marked_message_ids"].append(message_id)
                elif state.get("isRead") is False:
                    outcome["failed_message_ids"].append(message_id)
                else:
                    outcome["unknown_message_ids"].append(message_id)
            except IntegrationProviderError as exc:
                outcome["unknown_message_ids"].append(message_id)
                errors.append({
                    "error_type": exc.error_type,
                    "message": str(exc),
                    "retry_after_seconds": exc.retry_after_seconds,
                })

    def _send(self, input_json: dict[str, Any], access_token: str) -> dict[str, Any]:
        recipients = self._recipients(input_json)
        subject = _bounded(input_json.get("subject"), "subject", 500)
        body = _bounded(input_json.get("body"), "body", 20_000)
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": value}} for value in recipients["to"]],
            "ccRecipients": [{"emailAddress": {"address": value}} for value in recipients["cc"]],
            "bccRecipients": [{"emailAddress": {"address": value}} for value in recipients["bcc"]],
        }
        draft = self._request(f"{OUTLOOK_GRAPH_BASE}/me/messages", method="POST", access_token=access_token, body=message)
        message_id = _bounded(draft.get("id"), "message id", provider=True)
        conversation_id = _bounded(draft.get("conversationId"), "conversation id", provider=True)
        try:
            self._request(f"{OUTLOOK_GRAPH_BASE}/me/messages/{quote(message_id, safe='')}/send", method="POST", access_token=access_token)
        except IntegrationProviderError as exc:
            raise IntegrationProviderError(
                exc.error_type if exc.error_type != "invalid_input" else "provider_unavailable",
                "Outlook send did not complete; an unsent draft or unknown delivery state may remain",
                retry_after_seconds=exc.retry_after_seconds,
            ) from None
        return {"provider": "outlook", "sent": True, "message_id": message_id, "conversation_id": conversation_id}

    def _conversation_summary(self, conversation_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        latest = max(messages, key=lambda item: utc_rfc3339(item.get("receivedDateTime"), fallback=item.get("sentDateTime")))
        return {
            "provider": "outlook",
            "conversation_id": conversation_id,
            "subject": str(latest.get("subject") or "")[:2000],
            "latest_sender": self._address(latest.get("from")),
            "latest_timestamp": utc_rfc3339(latest.get("receivedDateTime"), fallback=latest.get("sentDateTime")),
            "snippet": str(latest.get("bodyPreview") or "")[:2000],
            "message_count": min(len(messages), MAX_CONVERSATION_MESSAGES),
            "unread": any(not bool(item.get("isRead", True)) for item in messages),
            "has_attachment": any(bool(item.get("hasAttachments")) for item in messages),
        }

    def _normalize_message(self, message: Any) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise IntegrationProviderError("provider_unavailable", "Microsoft returned an invalid message")
        message_id = _bounded(message.get("id"), "message id", provider=True)
        conversation_id = _bounded(message.get("conversationId"), "conversation id", provider=True)
        body = message.get("body")
        text = body.get("content", "") if isinstance(body, dict) else ""
        if not isinstance(text, str) or len(text) > 4_000_000:
            raise IntegrationProviderError("response_too_large", "Microsoft returned an oversized message body")
        return {
            "provider": "outlook",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "from": self._address(message.get("from")),
            "to": self._addresses(message.get("toRecipients")),
            "cc": self._addresses(message.get("ccRecipients")),
            "bcc": self._addresses(message.get("bccRecipients")),
            "subject": str(message.get("subject") or "")[:2000],
            "timestamp": utc_rfc3339(message.get("receivedDateTime"), fallback=message.get("sentDateTime")),
            "snippet": str(message.get("bodyPreview") or "")[:2000],
            "text": text,
            "unread": not bool(message.get("isRead", True)),
            "attachments": self._attachments(message.get("attachments")),
        }

    @staticmethod
    def _address(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        email = value.get("emailAddress")
        if not isinstance(email, dict):
            return ""
        address = email.get("address")
        return str(address)[:2000] if isinstance(address, str) else ""

    @classmethod
    def _addresses(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [address for item in value[:100] if (address := cls._address(item))]

    @staticmethod
    def _attachments(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for item in value[:100]:
            if not isinstance(item, dict):
                continue
            filename = item.get("name")
            size = item.get("size", 0)
            if not isinstance(filename, str) or not filename or not isinstance(size, int) or size < 0:
                continue
            result.append({
                "filename": filename[:1024],
                "mime_type": str(item.get("contentType") or "application/octet-stream")[:255],
                "size": size,
            })
        return result

    @staticmethod
    def _recipients(input_json: dict[str, Any]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for field in ("to", "cc", "bcc"):
            value = input_json.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise IntegrationProviderError("invalid_input", f"{field} must contain email addresses")
            parsed = [address for _name, address in getaddresses(value) if address]
            if len(parsed) != len(value) or any(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address) is None for address in parsed):
                raise IntegrationProviderError("invalid_input", f"{field} contains an invalid email address")
            result[field] = parsed
        if not 1 <= len(result["to"]) <= 10:
            raise IntegrationProviderError("invalid_input", "to must contain 1 to 10 email addresses")
        if sum(len(value) for value in result.values()) > 20:
            raise IntegrationProviderError("invalid_input", "Email may have at most 20 total recipients")
        return result


def _bounded(value: Any, field: str, maximum: int = 1024, *, provider: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        error_type = "provider_unavailable" if provider else "invalid_input"
        raise IntegrationProviderError(error_type, f"{field} is invalid")
    return value


class FakeOutlookProviderAdapter(OutlookProviderAdapter):
    """Deterministic fake for contract and Graph behavior tests."""

    def __init__(self, *, email: str = "person@example.com", account_id: str = "outlook-account") -> None:
        self.email = email
        self.account_id = account_id
        self.error_type: str | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.conversations: dict[str, list[dict[str, Any]]] = {}
        self.sent_drafts: list[dict[str, Any]] = []

    def begin_authorization(
        self,
        client_id: str,
        client_secret: str,
        state_store: OutlookOAuthStateStore | None = None,
    ) -> tuple[str, str]:
        state, challenge = (state_store or outlook_oauth_state_store).create(client_id, client_secret)
        return state, outlook_authorization_url(client_id, state, challenge)

    def exchange_code(self, pending: PendingOutlookOAuth, code: str) -> dict[str, str]:
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Outlook OAuth failure")
        return {"access_token": f"access-{code}", "refresh_token": "fake-refresh-token"}

    def identity(self, access_token: str) -> dict[str, str]:
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Outlook identity failure")
        return {"account_id": self.account_id, "email": self.email}

    def execute(self, operation: IntegrationOperationSpec, input_json: dict[str, Any], credential: str) -> dict[str, Any]:
        del credential
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Outlook failure")
        self.calls.append((operation.id, dict(input_json)))
        if operation.id == "email.search":
            summaries = []
            for conversation_id, messages in self.conversations.items():
                if messages:
                    latest = messages[-1]
                    summaries.append({
                        "provider": "outlook",
                        "conversation_id": conversation_id,
                        "subject": latest.get("subject", ""),
                        "latest_sender": latest.get("from", ""),
                        "latest_timestamp": utc_rfc3339(latest.get("timestamp"), fallback=latest.get("date")),
                        "snippet": latest.get("snippet", ""),
                        "message_count": len(messages),
                        "unread": any(item.get("unread", False) for item in messages),
                        "has_attachment": any(item.get("attachments") for item in messages),
                    })
            return {"conversations": summaries, "has_more": False, "next_page_token": None, "provider_errors": []}
        if operation.id == "email.conversation.get":
            conversation_id = input_json["conversation_id"]
            if conversation_id not in self.conversations:
                raise IntegrationProviderError("not_found", "Fake Outlook conversation was not found")
            return {"provider": "outlook", "conversation_id": conversation_id, "messages": [
                {**item, "provider": "outlook", "timestamp": utc_rfc3339(item.get("timestamp"), fallback=item.get("date"))}
                for item in self.conversations[conversation_id][:100]
            ]}
        if operation.id in {"email.read_new", "email.read_and_mark_new"}:
            messages = [item for conversation in self.conversations.values() for item in conversation if item.get("unread", False)][:50]
            returned = [{**item, "provider": "outlook", "timestamp": utc_rfc3339(item.get("timestamp"), fallback=item.get("date"))} for item in messages]
            result: dict[str, Any] = {
                "messages": returned,
                "count": len(returned),
                "has_more": False,
                "provider_errors": [],
            }
            if operation.id == "email.read_and_mark_new":
                for item in messages:
                    item["unread"] = False
                result["mark_outcomes"] = [{
                    "provider": "outlook",
                    "marked_message_ids": [item["message_id"] for item in returned],
                    "marked_count": len(returned),
                    "failed_message_ids": [],
                    "failed_count": 0,
                    "unknown_message_ids": [],
                    "unknown_count": 0,
                }]
                result["provider_errors"] = []
            return result
        if operation.id == "email.send":
            index = len(self.sent_drafts) + 1
            self.sent_drafts.append(dict(input_json))
            return {"provider": "outlook", "sent": True, "message_id": f"outlook-message-{index}", "conversation_id": f"outlook-thread-{index}"}
        raise IntegrationProviderError("operation_undeclared", "Fake Outlook operation is not implemented")


__all__ = [
    "FakeOutlookProviderAdapter",
    "OUTLOOK_AUTHORITY",
    "OUTLOOK_CLIENT_SECRET_NAMESPACE",
    "OUTLOOK_GRAPH_BASE",
    "OUTLOOK_OAUTH_REDIRECT_URI",
    "OUTLOOK_OAUTH_RETURN_URL",
    "OUTLOOK_SCOPE",
    "OUTLOOK_SECRET_NAMESPACE",
    "OutlookOAuthStateStore",
    "OutlookProviderAdapter",
    "PendingOutlookOAuth",
    "UrllibOutlookTransport",
    "outlook_authorization_url",
    "outlook_oauth_state_store",
    "parse_outlook_credential",
    "serialize_outlook_credential",
]
