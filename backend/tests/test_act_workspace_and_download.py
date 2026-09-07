from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.services import act_workspace_service
from app.services.act_download_service import ActDownloadError, download_document
from app.services.act_workspace_service import (
    ensure_act_workspace,
    open_act_root,
)


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, body: bytes, *, media_type: str, url: str = "https://example.test/file.pdf") -> None:
        super().__init__(body)
        self.headers = {"Content-Type": media_type, "Content-Length": str(len(body))}
        self._url = url

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self._url


class FakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses

    def open(self, _request, timeout: int):  # noqa: ANN001, ANN201
        assert timeout == 30
        return self.responses.pop(0)


class FakeRedirect(FakeResponse):
    status = 302

    def __init__(self, location: str) -> None:
        super().__init__(b"", media_type="text/plain")
        self.headers["Location"] = location


def public_resolver(_host: str, _port: int, **_kwargs):
    return [(None, None, None, None, ("93.184.216.34", 443))]


def test_workspace_is_bootstrapped_with_empty_memory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.services.act_workspace_service.ACT_ROOT", tmp_path / "act")
    workspace = ensure_act_workspace()
    assert workspace.memory.is_dir()
    assert list(workspace.memory.iterdir()) == []
    assert workspace.quercus.is_dir()
    assert workspace.downloads.is_dir()
    assert not (workspace.root / "AGENTS.md").exists()


def test_workspace_preserves_agent_created_memory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.services.act_workspace_service.ACT_ROOT", tmp_path / "act")
    memory = tmp_path / "act" / "memory"
    memory.mkdir(parents=True)
    (memory / "unexpected.md").write_text("data", encoding="utf-8")
    workspace = ensure_act_workspace()
    assert (workspace.memory / "unexpected.md").read_text(encoding="utf-8") == "data"
    assert not (workspace.root / "AGENTS.md").exists()


def test_open_act_root_uses_only_the_managed_agent_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "act"
    opened: list[Path] = []
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", root)
    monkeypatch.setattr(act_workspace_service.sys, "platform", "win32")
    monkeypatch.setattr(
        act_workspace_service.os,
        "startfile",
        lambda path: opened.append(Path(path)),
        raising=False,
    )

    open_act_root()

    assert opened == [root.resolve()]
    assert (root / "memory").is_dir()
    assert (root / "knowledge" / "quercus").is_dir()
    assert (root / "workspace").is_dir()


def test_download_rejects_unsafe_file_type_before_network() -> None:
    with pytest.raises(ActDownloadError, match="not allowed"):
        download_document({"source": {"kind": "url", "url": "https://example.test/unsafe.exe"}})


def test_download_rejects_non_url_source() -> None:
    with pytest.raises(ActDownloadError, match="Only a URL"):
        download_document({"source": {"kind": "gmail", "id": "message"}})


def test_download_rejects_private_resolution() -> None:
    def resolver(_host: str, _port: int, **_kwargs):
        return [(None, None, None, None, ("127.0.0.1", 80))]

    with pytest.raises(ActDownloadError, match="private or local"):
        download_document(
            {"source": {"kind": "url", "url": "https://example.test/file.pdf"}},
            opener=FakeOpener([]),
            resolver=resolver,
        )


def test_download_revalidates_redirect_destination() -> None:
    def resolver(host: str, _port: int, **_kwargs):
        address = "127.0.0.1" if host == "local.test" else "93.184.216.34"
        return [(None, None, None, None, (address, 443))]

    with pytest.raises(ActDownloadError, match="private or local"):
        download_document(
            {"source": {"kind": "url", "url": "https://example.test/file.pdf"}},
            opener=FakeOpener([FakeRedirect("http://local.test/file.pdf")]),
            resolver=resolver,
        )


def test_download_validates_pdf_and_uses_collision_safe_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("app.services.act_workspace_service.ACT_ROOT", tmp_path / "act")
    first = download_document(
        {"source": {"kind": "url", "url": "https://example.test/file.pdf"}},
        opener=FakeOpener([FakeResponse(b"%PDF-1.7\nfirst", media_type="application/pdf")]),
        resolver=public_resolver,
    )
    second = download_document(
        {"source": {"kind": "url", "url": "https://example.test/file.pdf"}},
        opener=FakeOpener([FakeResponse(b"%PDF-1.7\nsecond", media_type="application/pdf")]),
        resolver=public_resolver,
    )
    assert first["path"] == "workspace/downloads/file.pdf"
    assert first["media_type"] == "application/pdf"
    assert second["filename"] == "file (1).pdf"


def test_download_rejects_mime_and_signature_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("app.services.act_workspace_service.ACT_ROOT", tmp_path / "act")
    with pytest.raises(ActDownloadError, match="content type"):
        download_document(
            {"source": {"kind": "url", "url": "https://example.test/file.pdf"}},
            opener=FakeOpener([FakeResponse(b"<html>", media_type="text/html")]),
            resolver=public_resolver,
        )
    with pytest.raises(ActDownloadError, match="valid PDF"):
        download_document(
            {"source": {"kind": "url", "url": "https://example.test/file.pdf"}},
            opener=FakeOpener([FakeResponse(b"not a pdf", media_type="application/pdf")]),
            resolver=public_resolver,
        )
