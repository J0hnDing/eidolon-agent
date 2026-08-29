from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import IntegrationConnection
from app.services.google_calendar_provider import (
    GOOGLE_CALENDAR_SECRET_NAMESPACE,
    FakeGoogleCalendarProviderAdapter,
    GoogleOAuthStateStore,
)
from app.services.integration_service import IntegrationError, IntegrationService
from app.services.secret_store import FakeSecretStore, SecretStoreError, WindowsCredentialSecretStore

CLIENT_SECRET_SENTINEL = "EIDOLON_GOOGLE_CLIENT_SECRET_6f5f"


def test_windows_secret_store_has_google_calendar_namespace() -> None:
    store = object.__new__(WindowsCredentialSecretStore)
    assert store._target("a" * 32, GOOGLE_CALENDAR_SECRET_NAMESPACE) == "Eidolon/GoogleCalendar/" + "a" * 32


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def connected_service(db: Session) -> tuple[IntegrationService, FakeSecretStore, FakeGoogleCalendarProviderAdapter]:
    store = FakeSecretStore()
    provider = FakeGoogleCalendarProviderAdapter()
    service = IntegrationService(
        db,
        secret_store=store,
        google_calendar=provider,
        google_oauth_states=GoogleOAuthStateStore(),
    )
    authorization_url = service.start_google_calendar_oauth("client-id", CLIENT_SECRET_SENTINEL)
    state = authorization_url.rsplit("state=", 1)[1]
    service.complete_google_calendar_oauth(state, "authorization-code")
    return service, store, provider


def test_oauth_connection_is_secret_free_and_atomic(db: Session) -> None:
    service, store, provider = connected_service(db)
    status = service.google_calendar_connection_status()

    assert status.connected is True
    assert status.account_email == "person@example.com"
    assert CLIENT_SECRET_SENTINEL not in status.model_dump_json()
    connection = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "google_calendar"))
    assert connection is not None
    assert connection.credential_kind == "oauth_refresh"
    assert CLIENT_SECRET_SENTINEL not in repr(connection.__dict__)
    stored = store.get(connection.secret_reference, namespace=GOOGLE_CALENDAR_SECRET_NAMESPACE)
    assert CLIENT_SECRET_SENTINEL in stored

    provider.error_type = "invalid_credential"
    replacement_url = service.start_google_calendar_oauth("replacement-id", "replacement-secret")
    replacement_state = replacement_url.rsplit("state=", 1)[1]
    with pytest.raises(IntegrationError):
        service.complete_google_calendar_oauth(replacement_state, "bad-code")
    assert service.google_calendar_connection_status().account_email == "person@example.com"
    assert store.get(connection.secret_reference, namespace=GOOGLE_CALENDAR_SECRET_NAMESPACE) == stored


def test_direct_calls_use_five_operations_and_audit_only_event_id(db: Session) -> None:
    service, _store, provider = connected_service(db)
    start = {"date": "2026-09-01"}
    end = {"date": "2026-09-02"}
    create_audit = SimpleNamespace(resource=None)
    created = service.invoke_direct(
        "google_calendar.event.create",
        {"title": "All day", "start": start, "end": end, "recurrence": ["RRULE:FREQ=YEARLY"]},
        audit_record=create_audit,
    )
    listed = service.invoke_direct("google_calendar.event.list", {"time_min": "2026-08-01T00:00:00Z"})
    audit = SimpleNamespace(resource=None)
    fetched = service.invoke_direct("google_calendar.event.get", {"id": created["id"]}, audit_record=audit)
    updated = service.invoke_direct(
        "google_calendar.event.update",
        {"id": created["id"], "description": "Updated"},
    )
    deleted = service.invoke_direct("google_calendar.event.delete", {"id": created["id"]})

    assert listed["events"][0]["id"] == created["id"]
    assert fetched["id"] == created["id"]
    assert updated["description"] == "Updated"
    assert deleted == {"id": created["id"], "deleted": True}
    assert audit.resource == f"google-calendar-event:{created['id']}"
    assert create_audit.resource == f"google-calendar-event:{created['id']}"
    assert [call[0] for call in provider.calls] == [
        "google_calendar.event.create",
        "google_calendar.event.list",
        "google_calendar.event.get",
        "google_calendar.event.update",
        "google_calendar.event.delete",
    ]


def test_invalid_refresh_marks_connection_invalid(db: Session) -> None:
    service, _store, provider = connected_service(db)
    provider.error_type = "invalid_credential"

    with pytest.raises(IntegrationError) as exc_info:
        service.invoke_direct("google_calendar.event.list", {"time_min": "2026-08-01T00:00:00Z"})

    assert exc_info.value.error_type == "invalid_credential"
    assert service.google_calendar_connection_status().status == "invalid"


def test_remove_deletes_only_local_google_credential(db: Session) -> None:
    service, store, _provider = connected_service(db)
    connection = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "google_calendar"))
    assert connection is not None
    reference = connection.secret_reference

    removed = service.remove_google_calendar_connection()

    assert removed.status == "disconnected"
    assert db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "google_calendar")) is None
    with pytest.raises(SecretStoreError):
        store.get(reference, namespace=GOOGLE_CALENDAR_SECRET_NAMESPACE)
