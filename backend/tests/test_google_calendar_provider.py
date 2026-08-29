import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.github_provider import IntegrationProviderError
from app.services.google_calendar_provider import (
    GOOGLE_CALENDAR_SCOPE,
    GOOGLE_OAUTH_REDIRECT_URI,
    GoogleOAuthStateStore,
    PendingGoogleOAuth,
    UrllibGoogleCalendarProviderAdapter,
)
from app.services.integration_registry import OPERATIONS


def google_event(**overrides):
    value = {
        "id": "event-1",
        "status": "confirmed",
        "summary": "Planning",
        "description": "Weekly planning",
        "location": "Room 1",
        "start": {"dateTime": "2026-09-01T09:00:00-04:00", "timeZone": "America/Toronto"},
        "end": {"dateTime": "2026-09-01T10:00:00-04:00", "timeZone": "America/Toronto"},
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
        "htmlLink": "https://calendar.google.com/event?eid=event-1",
        "created": "2026-08-01T00:00:00Z",
        "updated": "2026-08-02T00:00:00Z",
    }
    value.update(overrides)
    return value


def credential() -> str:
    return json.dumps(
        {"client_id": "client-id", "client_secret": "client-secret", "refresh_token": "refresh-token"}
    )


def test_oauth_url_exchange_and_identity_use_exact_local_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibGoogleCalendarProviderAdapter()
    url = adapter.authorization_url("client-id", "state-value")
    query = parse_qs(urlsplit(url).query)
    assert query == {
        "access_type": ["offline"],
        "client_id": ["client-id"],
        "include_granted_scopes": ["false"],
        "prompt": ["consent"],
        "redirect_uri": [GOOGLE_OAUTH_REDIRECT_URI],
        "response_type": ["code"],
        "scope": [f"openid email {GOOGLE_CALENDAR_SCOPE}"],
        "state": ["state-value"],
    }
    responses = iter(
        [
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "scope": f"openid email {GOOGLE_CALENDAR_SCOPE}",
            },
            {"sub": "stable-subject", "email": "person@example.com", "email_verified": True},
        ]
    )
    monkeypatch.setattr(adapter, "_request_json", lambda *_args, **_kwargs: next(responses))

    tokens = adapter.exchange_code(PendingGoogleOAuth("client-id", "client-secret", 100), "code")
    identity = adapter.identity(tokens["access_token"])

    assert tokens == {"access_token": "access-token", "refresh_token": "refresh-token"}
    assert identity["email"] == "person@example.com"
    assert len(identity["account_id"]) == 64


def test_oauth_state_is_single_use_and_bounded() -> None:
    states = GoogleOAuthStateStore(ttl_seconds=60, max_pending=1)
    first = states.create("first", "secret")
    second = states.create("second", "secret")

    with pytest.raises(IntegrationProviderError):
        states.consume(first)
    assert states.consume(second).client_id == "second"
    with pytest.raises(IntegrationProviderError):
        states.consume(second)


def test_create_list_update_and_delete_use_only_primary_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibGoogleCalendarProviderAdapter()
    monkeypatch.setattr(adapter, "_refresh_access_token", lambda _bundle: "access-token")
    calls: list[tuple[str, str, bytes | None]] = []
    responses = iter(
        [
            google_event(),
            {"items": [google_event(recurrence=None, recurringEventId="series-1")], "nextPageToken": "next"},
            google_event(),
            google_event(location="Room 2"),
        ]
    )

    def request_json(url, *, method, body, **_kwargs):
        calls.append((method, url, body))
        return next(responses)

    def request_bytes(url, *, method, body, **_kwargs):
        calls.append((method, url, body))
        return b""

    monkeypatch.setattr(adapter, "_request_json", request_json)
    monkeypatch.setattr(adapter, "_request_bytes", request_bytes)
    start = {"date_time": "2026-09-01T09:00:00-04:00", "time_zone": "America/Toronto"}
    end = {"date_time": "2026-09-01T10:00:00-04:00", "time_zone": "America/Toronto"}

    created = adapter.execute(
        OPERATIONS["google_calendar.event.create"],
        {"title": "Planning", "start": start, "end": end, "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"]},
        credential(),
    )
    listed = adapter.execute(
        OPERATIONS["google_calendar.event.list"],
        {"time_min": "2026-09-01T00:00:00Z", "page_size": 25},
        credential(),
    )
    fetched = adapter.execute(
        OPERATIONS["google_calendar.event.get"],
        {"id": "event-1"},
        credential(),
    )
    updated = adapter.execute(
        OPERATIONS["google_calendar.event.update"],
        {"id": "event-1", "location": "Room 2"},
        credential(),
    )
    deleted = adapter.execute(
        OPERATIONS["google_calendar.event.delete"],
        {"id": "event-1"},
        credential(),
    )

    assert created["recurrence"] == ["RRULE:FREQ=WEEKLY;COUNT=4"]
    assert listed["events"][0]["recurring_event_id"] == "series-1"
    assert listed["has_more"] is True
    assert fetched["id"] == "event-1"
    assert updated["location"] == "Room 2"
    assert deleted == {"id": "event-1", "deleted": True}
    assert [method for method, _url, _body in calls] == ["POST", "GET", "GET", "PATCH", "DELETE"]
    assert all("/calendars/primary/events" in url for _method, url, _body in calls)
    list_query = parse_qs(urlsplit(calls[1][1]).query)
    assert list_query["singleEvents"] == ["true"]
    assert list_query["orderBy"] == ["startTime"]
    assert list_query["eventTypes"] == ["default"]


@pytest.mark.parametrize(
    "input_json",
    [
        {
            "title": "Bad",
            "start": {"date": "2026-09-02"},
            "end": {"date": "2026-09-01"},
        },
        {
            "title": "Bad",
            "start": {"date_time": "2026-09-01T09:00:00-04:00", "time_zone": "America/Toronto"},
            "end": {"date_time": "2026-09-01T10:00:00-04:00", "time_zone": "Europe/London"},
        },
        {
            "title": "Bad",
            "start": {"date": "2026-09-01"},
            "end": {"date": "2026-09-02"},
            "recurrence": ["DTSTART:20260901"],
        },
    ],
)
def test_invalid_time_and_recurrence_are_rejected_before_provider_call(
    input_json: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UrllibGoogleCalendarProviderAdapter()
    monkeypatch.setattr(adapter, "_refresh_access_token", lambda _bundle: "access-token")
    monkeypatch.setattr(adapter, "_request_json", lambda *_args, **_kwargs: pytest.fail("provider called"))

    with pytest.raises(IntegrationProviderError) as exc_info:
        adapter.execute(OPERATIONS["google_calendar.event.create"], input_json, credential())

    assert exc_info.value.error_type == "invalid_input"


def test_paginated_list_requires_reused_time_min(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibGoogleCalendarProviderAdapter()
    monkeypatch.setattr(adapter, "_refresh_access_token", lambda _bundle: "access-token")

    with pytest.raises(IntegrationProviderError) as exc_info:
        adapter.execute(
            OPERATIONS["google_calendar.event.list"],
            {"page_token": "next"},
            credential(),
        )

    assert exc_info.value.error_type == "invalid_input"
