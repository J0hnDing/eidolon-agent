from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.services import act_workspace_service
from app.services.github_provider import IntegrationProviderError
from app.services.google_drive_provider import GOOGLE_DRIVE_SECRET_NAMESPACE, GoogleDriveClient
from app.services.google_oauth import GoogleOAuthStateStore
from app.services.integration_service import IntegrationError, IntegrationService
from app.services.secret_store import FakeSecretStore, WindowsCredentialSecretStore


class FakeDriveClient(GoogleDriveClient):
    def __init__(self, metadata: dict) -> None:
        super().__init__()
        self.metadata = metadata
        self.requests: list[tuple[str, str, bytes | None]] = []

    def _request_json(self, url, *, method, headers, body, timeout, max_bytes, oauth_request=False):
        self.requests.append((method, url, body))
        if url.endswith("/token"):
            return {"access_token": "access"}
        if method == "POST":
            return self.metadata
        if urlsplit(url).path.endswith("/files"):
            return {"files": [self.metadata]}
        return self.metadata

    def _request_bytes(self, url, *, method, headers, body, timeout, max_bytes, oauth_request=False):
        self.requests.append((method, url, body))
        return b"hello Drive"


class FakeDriveOAuthClient(FakeDriveClient):
    def authorization_url(self, client_id: str, state: str) -> str:
        return f"https://accounts.google.com/?state={state}"

    def exchange_code(self, pending, code: str) -> dict[str, str]:
        return {"access_token": "access", "refresh_token": "refresh"}

    def identity(self, access_token: str) -> dict[str, str]:
        return {"account_id": "drive-account", "email": "drive@example.com"}


def _credential() -> str:
    return json.dumps({"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"})


def test_windows_store_has_separate_drive_namespace() -> None:
    store = object.__new__(WindowsCredentialSecretStore)
    assert store._target("a" * 32, GOOGLE_DRIVE_SECRET_NAMESPACE) == "Eidolon/GoogleDrive/" + "a" * 32


def test_drive_oauth_uses_shared_client_but_separate_grant(tmp_path: Path) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = FakeSecretStore()
        service = IntegrationService(
            db, project_root=tmp_path, secret_store=store,
            google_drive=FakeDriveOAuthClient(_metadata()),
            google_drive_oauth_states=GoogleOAuthStateStore(),
        )
        service.configure_google_oauth_client("client", "secret")
        state = parse_qs(urlsplit(service.start_google_drive_oauth()).query)["state"][0]
        status = service.complete_google_drive_oauth(state, "code")
        assert status.connected and status.account_email == "drive@example.com"
        assert sorted(store.namespaces.values()) == ["google_drive", "google_oauth"]
        with pytest.raises(IntegrationError):
            service.remove_google_oauth_client()
        service.remove_google_drive_connection()
        assert service.google_drive_connection_status().status == "disconnected"


def _metadata(mime: str = "text/plain", name: str = "note.txt") -> dict:
    return {"id": "drive-id", "name": name, "mimeType": mime, "parents": ["folder"], "modifiedTime": "2026-09-30T12:00:00Z", "webViewLink": "https://drive.google.com/file/d/drive-id/view"}


def test_search_escapes_filters_and_lists_folder() -> None:
    client = FakeDriveClient(_metadata())
    result = client.execute("google_drive.search", {"query": "Bob's notes", "parent_id": "folder", "limit": 5}, _credential())
    assert result["files"][0]["mime_type"] == "text/plain"
    query = parse_qs(urlsplit(client.requests[-1][1]).query)["q"][0]
    assert "name contains 'Bob\\'s notes'" in query
    assert "'folder' in parents" in query
    assert "trashed = false" in query


def test_download_and_read_use_knowledge_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path)
    client = FakeDriveClient(_metadata())
    result = client.execute("google_drive.download", {"file_id": "drive-id"}, _credential())
    assert result["file"]["path"].startswith("knowledge/google drive download/")
    assert (tmp_path / result["file"]["path"]).read_bytes() == b"hello Drive"
    read = client.execute("google_drive.read", {"file_id": "drive-id"}, _credential())
    assert read["text"] == "hello Drive"


def test_google_native_file_uses_export_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path)
    client = FakeDriveClient(_metadata("application/vnd.google-apps.document", "draft"))
    result = client.execute("google_drive.download", {"file_id": "drive-id", "export_format": "txt"}, _credential())
    assert result["file"]["filename"] == "draft.txt"
    assert "/export?" in client.requests[-1][1]
    with pytest.raises(IntegrationProviderError) as exc:
        client.execute("google_drive.download", {"file_id": "drive-id", "export_format": "xlsx"}, _credential())
    assert exc.value.error_type == "invalid_input"


def test_upload_accepts_local_reference_and_rejects_external_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path / "act")
    local = tmp_path / "act" / "workspace" / "downloads" / "note.txt"
    local.parent.mkdir(parents=True)
    local.write_text("hello", encoding="utf-8")
    client = FakeDriveClient(_metadata())
    reference = {"path": "workspace/downloads/note.txt", "filename": "note.txt", "bytes": 5, "media_type": "text/plain"}
    assert client.execute("google_drive.upload", {"file": reference, "parent_id": "folder"}, _credential())["id"] == "drive-id"
    assert client.requests[-1][0] == "POST"
    with pytest.raises(IntegrationProviderError) as exc:
        client.execute("google_drive.upload", {"file": {**reference, "path": "../../private.txt"}}, _credential())
    assert exc.value.error_type == "invalid_input"
    memory = tmp_path / "act" / "memory" / "secret.txt"
    memory.parent.mkdir(exist_ok=True)
    memory.write_text("hello", encoding="utf-8")
    with pytest.raises(IntegrationProviderError) as exc:
        client.execute("google_drive.upload", {"file": {**reference, "path": "memory/secret.txt", "filename": "secret.txt"}}, _credential())
    assert exc.value.error_type == "invalid_input"
