from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.routers import integrations
from app.schemas.integration import GitHubCredentialWrite
from app.services.github_provider import FakeGitHubProviderAdapter
from app.services.integration_service import IntegrationService
from app.services.secret_store import FakeSecretStore

SENTINEL = "EIDOLON_GITHUB_ROUTE_SENTINEL_d7d7"


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
    provider = FakeGitHubProviderAdapter(login="route-octocat", account_id="99")
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=FakeSecretStore(),
        github=provider,
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

    paths = app.openapi()["paths"]
    assert "/settings/integrations/github" in paths
    assert "/integrations/capabilities/invoke" not in paths
    assert all("github.repository." not in path for path in paths)
