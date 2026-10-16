from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.services import act_workspace_service, google_drive_provider
from app.services.github_provider import IntegrationProviderError
from app.services.google_drive_provider import GOOGLE_DRIVE_SECRET_NAMESPACE, GoogleDriveClient
from app.services.google_oauth import GoogleOAuthStateStore
from app.services.integration_service import IntegrationError, IntegrationService
from app.services.secret_store import FakeSecretStore, WindowsCredentialSecretStore


class FakeDriveClient(GoogleDriveClient):
    def __init__(self, metadata: dict, payload: bytes = b"hello Drive", declared_size: int | None = None) -> None:
        super().__init__()
        self.metadata = metadata
        self.requests: list[tuple[str, str, bytes | None]] = []
        self._opener = FakeDownloadOpener(self.requests, payload, declared_size)

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


class FakeDownloadResponse(BytesIO):
    def __init__(self, payload: bytes, declared_size: int | None) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(declared_size if declared_size is not None else len(payload))}


class FakeDownloadOpener:
    def __init__(self, requests: list, payload: bytes, declared_size: int | None) -> None:
        self.requests = requests
        self.payload = payload
        self.declared_size = declared_size

    def open(self, request, timeout):
        self.requests.append((request.get_method(), request.full_url, None))
        assert request.get_header("Authorization") == "Bearer access"
        return FakeDownloadResponse(self.payload, self.declared_size)


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
    assert result["file"]["path"].startswith("knowledge/google_drive_download/")
    assert (tmp_path / result["file"]["path"]).read_bytes() == b"hello Drive"
    read = client.execute("google_drive.read", {"file_id": "drive-id"}, _credential())
    assert read["text"] == "hello Drive"


def test_download_accepts_tex_and_arbitrary_binary_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path)
    for mime, name, payload in (
        ("text/x-tex", "resume.tex", b"\\documentclass{article}"),
        ("application/x-custom", "archive.custom", b"\x00\xff\x01"),
    ):
        client = FakeDriveClient(_metadata(mime, name), payload)
        result = client.execute("google_drive.download", {"file_id": "drive-id"}, _credential())
        assert result["file"]["filename"] == name
        assert result["file"]["media_type"] == mime
        assert result["file"]["bytes"] == len(payload)
        assert (tmp_path / result["file"]["path"]).read_bytes() == payload
        assert client.requests[-1][1].endswith("?alt=media")
        if name.endswith(".tex"):
            assert client.execute("google_drive.read", {"file_id": "drive-id"}, _credential())["text"] == payload.decode()


def test_download_stream_limit_removes_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path)
    monkeypatch.setattr(google_drive_provider, "MAX_DRIVE_DOWNLOAD_BYTES", 5)
    client = FakeDriveClient(_metadata(), b"123456")
    with pytest.raises(IntegrationProviderError) as exc:
        client.execute("google_drive.download", {"file_id": "drive-id"}, _credential())
    assert exc.value.error_type == "response_too_large"
    folder = tmp_path / "knowledge" / "google_drive_download"
    assert not list(folder.iterdir())

    client = FakeDriveClient(_metadata(), b"123456", declared_size=5)
    with pytest.raises(IntegrationProviderError) as exc:
        client.execute("google_drive.download", {"file_id": "drive-id"}, _credential())
    assert exc.value.error_type == "response_too_large"
    assert not list(folder.iterdir())


def test_read_has_smaller_limit_than_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path)
    monkeypatch.setattr(google_drive_provider, "MAX_DRIVE_READ_BYTES", 5)
    client = FakeDriveClient(_metadata(), b"123456")
    with pytest.raises(IntegrationProviderError) as exc:
        client.execute("google_drive.read", {"file_id": "drive-id"}, _credential())
    assert exc.value.error_type == "response_too_large"
    assert not list((tmp_path / "knowledge" / "google_drive_download").iterdir())


def test_download_rejects_truncated_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path)
    client = FakeDriveClient(_metadata(), b"short", declared_size=10)
    with pytest.raises(IntegrationProviderError) as exc:
        client.execute("google_drive.download", {"file_id": "drive-id"}, _credential())
    assert exc.value.error_type == "provider_unavailable"
    assert not list((tmp_path / "knowledge" / "google_drive_download").iterdir())


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
