from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.routers import integrations
from app.schemas.integration import (
    ApiKeyCredentialWrite,
    BrowserAuthenticationSecretWrite,
    GitHubCredentialWrite,
    GoogleOAuthClientWrite,
    MicrosoftOAuthClientWrite,
    NotionCredentialWrite,
    NotionDailyFeedPageWrite,
    NotionDataSourcesWrite,
)
from app.services.browser_authentication_service import BrowserAuthenticationService
from app.services.daily_feed_page_service import FakeDailyFeedPageProvider
from app.services.github_provider import FakeGitHubProviderAdapter
from app.services.gmail_provider import FakeGmailProviderAdapter
from app.services.google_calendar_provider import FakeGoogleCalendarProviderAdapter, GoogleOAuthStateStore
from app.services.integration_service import IntegrationService
from app.services.outlook_provider import FakeOutlookProviderAdapter, OutlookOAuthStateStore
from app.services.report_service import FakeReportProvider
from app.services.secret_store import FakeSecretStore
from app.services.todo_service import FakeTodoProvider

SENTINEL = "EIDOLON_GITHUB_ROUTE_SENTINEL_d7d7"


def test_macro_api_key_routes_store_only_opaque_references(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import select

    from app.models import IntegrationConnection

    store = FakeSecretStore()
    service = IntegrationService(db, project_root=tmp_path, secret_store=store)
    monkeypatch.setattr(integrations, "build_default_integration_service", lambda _db: service)
    monkeypatch.setattr(integrations.FunctionCatalogService, "refresh", lambda _self: None)
    sentinel = "EIDOLON_FRED_ROUTE_SECRET_34d8"

    created = integrations.put_fred_api_key(ApiKeyCredentialWrite(api_key=sentinel), db)
    assert created.provider == "fred"
    assert created.configured is True
    assert created.available is True
    assert created.last_validated_at is not None
    serialized = created.model_dump_json()
    assert sentinel not in serialized
    assert "secret_reference" not in serialized

    row = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "fred"))
    assert row is not None
    assert store.get(row.secret_reference, namespace="fred") == sentinel
    assert integrations.fred_api_key_status(db).available is True

    replaced = integrations.put_fred_api_key(ApiKeyCredentialWrite(api_key="EIDOLON_FRED_ROUTE_SECRET_REPLACED"), db)
    assert replaced.last_validated_at is not None

    removed = integrations.remove_fred_api_key(db)
    assert removed.status_code == 204
    assert integrations.fred_api_key_status(db).status == "not_configured"


def test_browser_authentication_settings_routes_never_return_secrets(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeSecretStore()
    service = BrowserAuthenticationService(db, secret_store=store)
    monkeypatch.setattr(
        integrations,
        "build_default_browser_authentication_service",
        lambda _db: service,
    )
    username = "BROWSER_ROUTE_USERNAME_SENTINEL"
    password = "BROWSER_ROUTE_PASSWORD_SENTINEL"

    created = integrations.put_browser_authentication_secret(
        "uoft",
        BrowserAuthenticationSecretWrite(username=username, password=password),
        db,
    )

    serialized = created.model_dump_json()
    assert created.identities[0].configured is True
    assert username not in serialized
    assert password not in serialized
    assert "secret_reference" not in serialized
    status = integrations.browser_authentication_status(db)
    assert username not in status.model_dump_json()
    assert password not in status.model_dump_json()

    removed = integrations.remove_browser_authentication_secret("uoft", db)
    assert removed.status_code == 204
    assert integrations.browser_authentication_status(db).identities[0].configured is False


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


def test_trusted_settings_routes_are_sanitized_and_internal_relay_is_hidden(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(integrations.FunctionCatalogService, "refresh", lambda _self: None)
    provider = FakeGitHubProviderAdapter(login="route-octocat", account_id="99")
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=FakeSecretStore(),
        github=provider,
        google_calendar=FakeGoogleCalendarProviderAdapter(),
        google_oauth_states=GoogleOAuthStateStore(),
        gmail=FakeGmailProviderAdapter(email="mail@example.com", account_id="gmail-route"),
        gmail_oauth_states=GoogleOAuthStateStore(),
        outlook=FakeOutlookProviderAdapter(email="school@example.edu", account_id="outlook-route"),
        outlook_oauth_states=OutlookOAuthStateStore(),
    )
    monkeypatch.setattr(integrations, "build_default_integration_service", lambda _db: service)
    app = FastAPI()
    app.include_router(integrations.router)

    created = integrations.put_github_connection(GitHubCredentialWrite(token=SENTINEL), db)
    assert created.account_login == "route-octocat"
    assert SENTINEL not in created.model_dump_json()

    connection_status = integrations.github_connection_status(db)
    assert connection_status.connected is True
    serialized_status = connection_status.model_dump_json()
    assert "secret_reference" not in serialized_status
    assert SENTINEL not in serialized_status

    provider.error_type = "invalid_credential"
    with pytest.raises(HTTPException) as exc_info:
        integrations.put_github_connection(GitHubCredentialWrite(token="invalid-replacement"), db)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["type"] == "invalid_credential"
    assert "invalid-replacement" not in str(exc_info.value.detail)
    assert integrations.github_connection_status(db).account_login == "route-octocat"

    provider.error_type = None
    oversized = f"{SENTINEL}{'x' * 4096}"
    with pytest.raises(HTTPException) as exc_info:
        integrations.put_github_connection(GitHubCredentialWrite(token=oversized), db)
    assert exc_info.value.status_code == 422
    assert SENTINEL not in str(exc_info.value.detail)

    removed = integrations.remove_github_connection(db)
    assert removed.status_code == 204
    assert integrations.github_connection_status(db).status == "disconnected"

    notion_sentinel = "EIDOLON_NOTION_ROUTE_SENTINEL_71a4"
    service.notion_provider_factory = lambda _token, _source: FakeTodoProvider()
    service.notion_report_provider_factory = lambda _token, _source: FakeReportProvider()
    service.notion_daily_feed_provider_factory = (
        lambda _token, _page: FakeDailyFeedPageProvider()
    )
    notion_created = integrations.put_notion_connection(
        NotionCredentialWrite(token=notion_sentinel),
        db,
    )
    assert notion_created.connected is True
    assert notion_created.data_source_id is None
    assert notion_created.report_data_source_id is None
    assert notion_created.daily_feed_page_id is None
    assert notion_sentinel not in notion_created.model_dump_json()
    data_sources = integrations.put_notion_data_sources(
        NotionDataSourcesWrite(
            data_source_id="source-id",
            report_data_source_id="report-source-id",
        ),
        db,
    )
    assert data_sources.data_source_id == "source-id"
    assert data_sources.report_data_source_id == "report-source-id"
    daily_feed_page = integrations.put_notion_daily_feed_page(
        NotionDailyFeedPageWrite(page_id="daily-page-id"),
        db,
    )
    assert daily_feed_page.daily_feed_page_id == "daily-page-id"
    cleared = integrations.remove_notion_data_sources(db)
    assert cleared.connected is True
    assert cleared.data_source_id is None
    assert cleared.report_data_source_id is None
    assert cleared.daily_feed_page_id == "daily-page-id"
    cleared_daily_feed = integrations.remove_notion_daily_feed_page(db)
    assert cleared_daily_feed.daily_feed_page_id is None
    notion_removed = integrations.remove_notion_connection(db)
    assert notion_removed.status_code == 204
    assert integrations.notion_connection_status(db).status == "disconnected"

    google_secret = "EIDOLON_GOOGLE_ROUTE_SENTINEL_95e1"
    shared_google = integrations.put_google_oauth_client(
        GoogleOAuthClientWrite(client_id="client-id", client_secret=google_secret),
        db,
    )
    assert shared_google.configured is True
    oauth_start = integrations.start_google_calendar_oauth(db)
    assert google_secret not in oauth_start.model_dump_json()
    state = oauth_start.authorization_url.rsplit("state=", 1)[1]
    callback = integrations.complete_google_calendar_oauth(state, "code", "", db)
    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "http://localhost:5174/settings/integrations?google_calendar=connected"
    )
    google_status = integrations.google_calendar_connection_status(db)
    assert google_status.connected is True
    assert google_status.account_email == "person@example.com"
    assert google_secret not in google_status.model_dump_json()
    denied_start = integrations.start_google_calendar_oauth(db)
    denied_state = denied_start.authorization_url.rsplit("state=", 1)[1]
    denied = integrations.complete_google_calendar_oauth(denied_state, "", "access_denied", db)
    assert denied.headers["location"] == (
        "http://localhost:5174/settings/integrations?google_calendar=denied"
    )
    invalid_denial = integrations.complete_google_calendar_oauth("invalid-state", "", "access_denied", db)
    assert invalid_denial.headers["location"] == (
        "http://localhost:5174/settings/integrations?google_calendar=failed"
    )
    assert integrations.google_calendar_connection_status(db).connected is True
    google_removed = integrations.remove_google_calendar_connection(db)
    assert google_removed.status_code == 204
    assert integrations.google_calendar_connection_status(db).status == "disconnected"

    gmail_start = integrations.start_gmail_oauth(db)
    assert google_secret not in gmail_start.model_dump_json()
    gmail_state = parse_qs(urlparse(gmail_start.authorization_url).query)["state"][0]
    gmail_callback = integrations.complete_gmail_oauth(gmail_state, "gmail-code", "", db)
    assert gmail_callback.headers["location"] == (
        "http://localhost:5174/settings/integrations?gmail=connected"
    )
    gmail_status = integrations.gmail_connection_status(db)
    assert gmail_status.connected is True
    assert gmail_status.account_email == "mail@example.com"
    assert service.google_calendar_connection_status().status == "disconnected"
    assert google_secret not in gmail_status.model_dump_json()
    assert integrations.remove_gmail_connection(db).status_code == 204
    removed_google = integrations.remove_google_oauth_client(db)
    assert removed_google.configured is False

    microsoft_secret = "EIDOLON_MICROSOFT_ROUTE_SECRET_5c3a"
    microsoft_status = integrations.put_microsoft_oauth_client(
        MicrosoftOAuthClientWrite(client_id="microsoft-client-id", client_secret=microsoft_secret),
        db,
    )
    assert microsoft_status.configured is True
    assert microsoft_secret not in microsoft_status.model_dump_json()
    outlook_start = integrations.start_outlook_oauth(db)
    outlook_state = parse_qs(urlparse(outlook_start.authorization_url).query)["state"][0]
    outlook_callback = integrations.complete_outlook_oauth(outlook_state, "outlook-code", "", db)
    assert outlook_callback.headers["location"] == (
        "http://localhost:5174/settings/integrations?outlook=connected"
    )
    outlook_status = integrations.outlook_connection_status(db)
    assert outlook_status.connected is True
    assert outlook_status.account_email == "school@example.edu"
    assert microsoft_secret not in outlook_status.model_dump_json()
    assert integrations.remove_outlook_connection(db).status_code == 204
    removed_microsoft = integrations.remove_microsoft_oauth_client(db)
    assert removed_microsoft.configured is False

    paths = app.openapi()["paths"]
    assert "/settings/integrations/github" in paths
    assert "/settings/integrations/notion" in paths
    assert "/settings/integrations/notion/data-sources" in paths
    assert "/settings/integrations/quercus/processing" in paths
    assert "/settings/integrations/quercus/processing/reprocess-failed" in paths
    assert "/settings/integrations/google" in paths
    assert "/settings/integrations/google/oauth-client" in paths
    assert "/settings/integrations/google-calendar" in paths
    assert "/settings/integrations/google-calendar/oauth/start" in paths
    assert "/settings/integrations/google-calendar/oauth/callback" not in paths
    assert "/settings/integrations/gmail" in paths
    assert "/settings/integrations/gmail/oauth/start" in paths
    assert "/settings/integrations/gmail/oauth/callback" not in paths
    assert "/settings/integrations/microsoft" in paths
    assert "/settings/integrations/microsoft/oauth-client" in paths
    assert "/settings/integrations/outlook" in paths
    assert "/settings/integrations/outlook/oauth/start" in paths
    assert "/settings/integrations/outlook/oauth/callback" not in paths
    assert "/settings/integrations/telegram" in paths
    assert "/integrations/capabilities/invoke" not in paths
    assert all("github.repository." not in path for path in paths)
