from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.integrations.types import IntegrationOperationSpec
from app.services.github_provider import IntegrationProviderError
from app.services.google_oauth import (
    MAX_OAUTH_RESPONSE_BYTES,
    GoogleOAuthStateStore,
    PendingGoogleOAuth,
    exchange_google_oauth_code,
    google_authorization_url,
    google_identity,
    parse_google_oauth_credential,
    refresh_google_access_token,
)

GOOGLE_CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"
GOOGLE_OAUTH_SCOPES = ("openid", "email", GOOGLE_CALENDAR_SCOPE)
GOOGLE_OAUTH_REDIRECT_URI = (
    "http://localhost:8000/settings/integrations/google-calendar/oauth/callback"
)
GOOGLE_OAUTH_RETURN_URL = "http://localhost:5174/settings/integrations"
GOOGLE_CALENDAR_SECRET_NAMESPACE = "google_calendar"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


google_oauth_state_store = GoogleOAuthStateStore()


class GoogleCalendarProviderAdapter(Protocol):
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
class GoogleCalendarTransportOperation:
    """Provider-private request limits, not semantic registry metadata."""

    operation_id: str
    timeout_seconds: float = 15
    max_provider_response_bytes: int = 2_000_000


_TRANSPORT_OPERATIONS = {
    operation_id: GoogleCalendarTransportOperation(operation_id)
    for operation_id in (
        "google_calendar.event.create",
        "google_calendar.event.list",
        "google_calendar.event.get",
        "google_calendar.event.update",
        "google_calendar.event.delete",
    )
}


def _transport_operation(operation: IntegrationOperationSpec) -> GoogleCalendarTransportOperation:
    try:
        return _TRANSPORT_OPERATIONS[operation.id]
    except KeyError:
        raise IntegrationProviderError(
            "operation_undeclared", "Google Calendar operation is not implemented"
        ) from None


class UrllibGoogleCalendarProviderAdapter:
    def __init__(self) -> None:
        self._opener = None

    def authorization_url(self, client_id: str, state: str) -> str:
        return google_authorization_url(
            client_id,
            state,
            redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
            scopes=GOOGLE_OAUTH_SCOPES,
        )

    def exchange_code(self, pending: PendingGoogleOAuth, code: str) -> dict[str, str]:
        return exchange_google_oauth_code(
            self._request_json,
            pending,
            code,
            redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
            required_scope=GOOGLE_CALENDAR_SCOPE,
            permission_name="Google Calendar",
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
        bundle = self._credential_bundle(credential)
        access_token = self._refresh_access_token(bundle)
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        operation_id = operation.operation_id
        if operation_id == "google_calendar.event.create":
            body = self._event_write_body(input_json, creating=True)
            payload = self._request_json(
                self._event_url(query={"sendUpdates": "none"}),
                method="POST",
                headers={**headers, "Content-Type": "application/json"},
                body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
            )
            return self._event(payload)
        if operation_id == "google_calendar.event.list":
            return self._list_events(operation, input_json, headers)
        if operation_id == "google_calendar.event.get":
            payload = self._request_json(
                self._event_url(input_json["id"]),
                method="GET",
                headers=headers,
                body=None,
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
            )
            return self._event(payload)
        if operation_id == "google_calendar.event.update":
            event_id = input_json["id"]
            body = self._event_write_body(input_json, creating=False)
            payload = self._request_json(
                self._event_url(event_id, query={"sendUpdates": "none"}),
                method="PATCH",
                headers={**headers, "Content-Type": "application/json"},
                body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
            )
            return self._event(payload)
        if operation_id == "google_calendar.event.delete":
            event_id = input_json["id"]
            self._request_bytes(
                self._event_url(event_id, query={"sendUpdates": "none"}),
                method="DELETE",
                headers=headers,
                body=None,
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
            )
            return {"id": event_id, "deleted": True}
        raise IntegrationProviderError("operation_undeclared", "Google Calendar operation is not implemented")

    def _list_events(
        self,
        operation: GoogleCalendarTransportOperation,
        input_json: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        effective_min = input_json.get("time_min") or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        minimum = self._rfc3339(effective_min, "time_min")
        maximum_value = input_json.get("time_max")
        maximum = self._rfc3339(maximum_value, "time_max") if maximum_value is not None else None
        if maximum is not None and maximum <= minimum:
            raise IntegrationProviderError("invalid_input", "time_max must be later than time_min")
        if input_json.get("page_token") and "time_min" not in input_json:
            raise IntegrationProviderError("invalid_input", "Paginated calls must reuse the returned time_min")
        query: dict[str, Any] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "showDeleted": "false",
            "eventTypes": "default",
            "maxResults": input_json.get("page_size", 25),
            "timeMin": effective_min,
        }
        if maximum_value is not None:
            query["timeMax"] = maximum_value
        if "query" in input_json:
            query["q"] = input_json["query"]
        if "page_token" in input_json:
            query["pageToken"] = input_json["page_token"]
        payload = self._request_json(
            self._event_url(query=query),
            method="GET",
            headers=headers,
            body=None,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise IntegrationProviderError("provider_unavailable", "Google returned an invalid event list")
        events = [self._event(item) for item in items]
        next_page_token = payload.get("nextPageToken")
        if next_page_token is not None and not isinstance(next_page_token, str):
            raise IntegrationProviderError("provider_unavailable", "Google returned an invalid page token")
        return {
            "events": events,
            "time_min": effective_min,
            "time_max": maximum_value,
            "has_more": bool(next_page_token),
            "next_page_token": next_page_token,
        }

    def _event_write_body(self, input_json: dict[str, Any], *, creating: bool) -> dict[str, Any]:
        has_start = "start" in input_json
        has_end = "end" in input_json
        if has_start != has_end:
            raise IntegrationProviderError("invalid_input", "start and end must be supplied together")
        body: dict[str, Any] = {}
        for name in ("description", "location"):
            if name in input_json:
                body[name] = "" if input_json[name] is None else input_json[name]
        if "title" in input_json:
            body["summary"] = input_json["title"]
        if has_start:
            start, start_value = self._event_time_input(input_json["start"], "start")
            end, end_value = self._event_time_input(input_json["end"], "end")
            if ("date" in start) != ("date" in end):
                raise IntegrationProviderError("invalid_input", "start and end must use the same time form")
            if "dateTime" in start and start["timeZone"] != end["timeZone"]:
                raise IntegrationProviderError("invalid_input", "start and end must use the same time zone")
            if end_value <= start_value:
                raise IntegrationProviderError("invalid_input", "end must be later than start")
            body["start"] = start
            body["end"] = end
        elif creating:
            raise IntegrationProviderError("invalid_input", "start and end are required")
        if "recurrence" in input_json:
            recurrence = input_json["recurrence"]
            if creating and not recurrence:
                raise IntegrationProviderError("invalid_input", "recurrence cannot be empty when creating an event")
            body["recurrence"] = self._recurrence(recurrence)
        return body

    def _event_time_input(self, value: Any, field: str) -> tuple[dict[str, str], datetime | date]:
        if not isinstance(value, dict):
            raise IntegrationProviderError("invalid_input", f"{field} is invalid")
        if set(value) == {"date"} and isinstance(value["date"], str):
            try:
                parsed = date.fromisoformat(value["date"])
            except ValueError:
                raise IntegrationProviderError("invalid_input", f"{field}.date is invalid") from None
            return {"date": value["date"]}, parsed
        if set(value) == {"date_time", "time_zone"} and all(isinstance(item, str) for item in value.values()):
            parsed = self._rfc3339(value["date_time"], f"{field}.date_time")
            try:
                ZoneInfo(value["time_zone"])
            except (ZoneInfoNotFoundError, ValueError):
                raise IntegrationProviderError("invalid_input", f"{field}.time_zone is invalid") from None
            return {"dateTime": value["date_time"], "timeZone": value["time_zone"]}, parsed
        raise IntegrationProviderError("invalid_input", f"{field} is invalid")

    def _recurrence(self, values: Any) -> list[str]:
        if not isinstance(values, list) or len(values) > 20:
            raise IntegrationProviderError("invalid_input", "recurrence is invalid")
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str) or not 1 <= len(value) <= 512:
                raise IntegrationProviderError("invalid_input", "recurrence is invalid")
            prefix = value.split(":", 1)[0].split(";", 1)[0].upper()
            if prefix not in {"RRULE", "RDATE", "EXDATE", "EXRULE"}:
                raise IntegrationProviderError("invalid_input", "recurrence contains an unsupported line")
            normalized.append(value)
        return normalized

    def _event(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("status") == "cancelled":
            raise IntegrationProviderError("not_found", "Google Calendar event was not found")
        event_id = payload.get("id")
        status = payload.get("status")
        created = payload.get("created")
        updated = payload.get("updated")
        if (
            not isinstance(event_id, str)
            or not event_id
            or status not in {"confirmed", "tentative"}
            or not isinstance(created, str)
            or not isinstance(updated, str)
        ):
            raise IntegrationProviderError("provider_unavailable", "Google returned an invalid event")
        return {
            "id": event_id,
            "status": status,
            "summary": self._optional_text(payload.get("summary"), 1024),
            "description": self._optional_text(payload.get("description"), 8192),
            "location": self._optional_text(payload.get("location"), 1024),
            "start": self._event_time_output(payload.get("start"), "start"),
            "end": self._event_time_output(payload.get("end"), "end"),
            "recurrence": self._recurrence_output(payload.get("recurrence")),
            "recurring_event_id": self._optional_text(payload.get("recurringEventId"), 1024),
            "original_start": (
                self._event_time_output(payload["originalStartTime"], "originalStartTime")
                if payload.get("originalStartTime") is not None
                else None
            ),
            "html_link": self._optional_text(payload.get("htmlLink"), 4096),
            "created_at": created,
            "updated_at": updated,
        }

    def _event_time_output(self, value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise IntegrationProviderError("provider_unavailable", f"Google returned an invalid {field}")
        if isinstance(value.get("date"), str):
            try:
                date.fromisoformat(value["date"])
            except ValueError:
                raise IntegrationProviderError("provider_unavailable", f"Google returned an invalid {field}") from None
            return {"date": value["date"]}
        date_time = value.get("dateTime")
        time_zone = value.get("timeZone")
        if not isinstance(date_time, str) or (time_zone is not None and not isinstance(time_zone, str)):
            raise IntegrationProviderError("provider_unavailable", f"Google returned an invalid {field}")
        self._rfc3339(date_time, field, provider_response=True)
        return {"date_time": date_time, "time_zone": time_zone}

    def _recurrence_output(self, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 20 or not all(isinstance(item, str) for item in value):
            raise IntegrationProviderError("provider_unavailable", "Google returned invalid recurrence data")
        if any(len(item) > 512 for item in value):
            raise IntegrationProviderError("provider_unavailable", "Google returned oversized recurrence data")
        return value

    def _credential_bundle(self, credential: str) -> dict[str, str]:
        return parse_google_oauth_credential(credential)

    def _refresh_access_token(self, bundle: dict[str, str]) -> str:
        return refresh_google_access_token(self._request_json, bundle)

    def _event_url(self, event_id: str | None = None, *, query: dict[str, Any] | None = None) -> str:
        path = f"{GOOGLE_CALENDAR_API_BASE}/calendars/primary/events"
        if event_id is not None:
            path = f"{path}/{quote(event_id, safe='')}"
        return f"{path}?{urlencode(query)}" if query else path

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
            payload = json.loads(raw.decode("utf-8"))
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
            error_type = "rate_limited" if self._rate_limited(exc) else "provider_forbidden"
        elif 500 <= exc.code <= 599:
            error_type = "provider_unavailable"
        else:
            error_type = "provider_unavailable"
        return IntegrationProviderError(error_type, "Google request failed", retry_after_seconds=retry_after)

    def _rate_limited(self, exc: HTTPError) -> bool:
        try:
            raw = exc.read(MAX_OAUTH_RESPONSE_BYTES)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        reasons = payload.get("error", {}).get("errors", []) if isinstance(payload, dict) else []
        return any(
            isinstance(item, dict)
            and item.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
            for item in reasons
        )

    @staticmethod
    def _retry_after(value: str | None) -> int | None:
        try:
            parsed = int(value or "")
        except ValueError:
            return None
        return parsed if 0 <= parsed <= 3600 else None

    @staticmethod
    def _optional_text(value: Any, max_length: int) -> str | None:
        if value in {None, ""}:
            return None
        if not isinstance(value, str) or len(value) > max_length:
            raise IntegrationProviderError("provider_unavailable", "Google returned invalid event text")
        return value

    @staticmethod
    def _rfc3339(value: Any, field: str, *, provider_response: bool = False) -> datetime:
        error_type = "provider_unavailable" if provider_response else "invalid_input"
        if not isinstance(value, str) or not value:
            raise IntegrationProviderError(error_type, f"{field} is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise IntegrationProviderError(error_type, f"{field} is invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise IntegrationProviderError(error_type, f"{field} must include a UTC offset")
        return parsed


class FakeGoogleCalendarProviderAdapter:
    def __init__(self, *, email: str = "person@example.com", account_id: str = "google-account") -> None:
        self.email = email
        self.account_id = account_id
        self.error_type: str | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events: dict[str, dict[str, Any]] = {}

    def authorization_url(self, client_id: str, state: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?client_id={quote(client_id)}&state={quote(state)}"

    def exchange_code(self, pending: PendingGoogleOAuth, code: str) -> dict[str, str]:
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Google OAuth failure")
        return {"access_token": f"access-{code}", "refresh_token": "fake-refresh-token"}

    def identity(self, access_token: str) -> dict[str, str]:
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Google identity failure")
        return {"account_id": self.account_id, "email": self.email}

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        operation = _transport_operation(operation)
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake Google Calendar failure")
        self.calls.append((operation.operation_id, input_json))
        if operation.operation_id == "google_calendar.event.list":
            time_min = input_json.get("time_min", "2026-09-01T00:00:00Z")
            return {
                "events": list(self.events.values()),
                "time_min": time_min,
                "time_max": input_json.get("time_max"),
                "has_more": False,
                "next_page_token": None,
            }
        if operation.operation_id == "google_calendar.event.get":
            event = self.events.get(input_json["id"])
            if event is None:
                raise IntegrationProviderError("not_found", "Fake event was not found")
            return event
        if operation.operation_id in {"google_calendar.event.create", "google_calendar.event.update"}:
            event_id = input_json.get("id", f"event-{len(self.events) + 1}")
            previous = self.events.get(event_id, {})
            event = {
                "id": event_id,
                "status": "confirmed",
                "summary": input_json.get("title", previous.get("summary")),
                "description": input_json.get("description", previous.get("description")),
                "location": input_json.get("location", previous.get("location")),
                "start": input_json.get("start", previous.get("start")),
                "end": input_json.get("end", previous.get("end")),
                "recurrence": input_json.get("recurrence", previous.get("recurrence", [])),
                "recurring_event_id": previous.get("recurring_event_id"),
                "original_start": previous.get("original_start"),
                "html_link": f"https://calendar.google.com/event?eid={event_id}",
                "created_at": previous.get("created_at", "2026-09-01T00:00:00Z"),
                "updated_at": "2026-09-01T00:00:00Z",
            }
            self.events[event_id] = event
            return event
        if operation.operation_id == "google_calendar.event.delete":
            self.events.pop(input_json["id"], None)
            return {"id": input_json["id"], "deleted": True}
        raise IntegrationProviderError("operation_undeclared", "Fake operation is not implemented")
