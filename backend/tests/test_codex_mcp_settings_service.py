from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import CodexMcpSettings
from app.services.codex_mcp_settings_service import (
    CodexMcpSettingsError,
    CodexMcpSettingsService,
)


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


@pytest.fixture(autouse=True)
def stub_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.codex_mcp_settings_service.McpFunctionService",
        lambda *_args, **_kwargs: SimpleNamespace(tool_count=7, excluded_ids=["backend.codex.call"]),
    )


def service(db: Session, root: Path, *, python_name: str = "python-a.exe") -> CodexMcpSettingsService:
    return CodexMcpSettingsService(
        db,
        codex_home=root / ".codex",
        backend_python=root / python_name,
        backend_directory=root / "backend",
        project_root=root,
    )


def test_install_repair_remove_preserve_unrelated_toml_and_are_idempotent(
    db: Session,
    tmp_path: Path,
) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "# keep this comment\nmodel = \"gpt-test\"\n\n[mcp_servers.other]\ncommand = \"other\"\n",
        encoding="utf-8",
    )
    first = service(db, tmp_path)

    installed = first.install()
    first_content = config.read_text(encoding="utf-8")
    repeated = first.install()

    assert installed.enabled is True
    assert installed.registered is True
    assert installed.config_matches is True
    assert installed.available_tool_count == 7
    assert repeated.config_matches is True
    assert config.read_text(encoding="utf-8") == first_content
    assert "# keep this comment" in first_content
    assert '[mcp_servers.other]' in first_content
    assert "enabled_tools" not in first_content
    assert "token" not in first_content.casefold()
    entry = tomlkit.parse(first_content)["mcp_servers"]["eidolon"].unwrap()
    assert entry["args"] == ["-m", "app.mcp_server"]
    assert entry["default_tools_approval_mode"] == "writes"

    repaired = service(db, tmp_path, python_name="python-b.exe").repair()
    assert repaired.config_matches is True
    assert "python-b.exe" in config.read_text(encoding="utf-8")

    removed = service(db, tmp_path, python_name="python-b.exe").remove()
    remaining = config.read_text(encoding="utf-8")
    assert removed.enabled is False
    assert removed.registered is False
    assert "eidolon" not in tomlkit.parse(remaining)["mcp_servers"]
    assert '[mcp_servers.other]' in remaining
    assert "# keep this comment" in remaining


def test_conflicting_entry_fails_closed_and_remove_revokes_before_refusing(
    db: Session,
    tmp_path: Path,
) -> None:
    configured = service(db, tmp_path)
    configured.install()
    config = configured.config_path
    document = tomlkit.parse(config.read_text(encoding="utf-8"))
    document["mcp_servers"]["eidolon"]["command"] = "changed-outside-eidolon.exe"
    config.write_text(tomlkit.dumps(document), encoding="utf-8")

    with pytest.raises(CodexMcpSettingsError) as repair_error:
        configured.repair()
    assert repair_error.value.error_type == "config_conflict"
    with pytest.raises(CodexMcpSettingsError) as remove_error:
        configured.remove()
    assert remove_error.value.error_type == "config_conflict"
    assert db.get(CodexMcpSettings, 1).enabled is False
    assert "changed-outside-eidolon.exe" in config.read_text(encoding="utf-8")


def test_preexisting_unowned_entry_is_never_claimed(db: Session, tmp_path: Path) -> None:
    configured = service(db, tmp_path)
    configured.config_path.parent.mkdir()
    configured.config_path.write_text(
        '[mcp_servers.eidolon]\ncommand = "someone-else"\n',
        encoding="utf-8",
    )

    with pytest.raises(CodexMcpSettingsError) as exc_info:
        configured.install()

    assert exc_info.value.error_type == "config_conflict"
    assert "someone-else" in configured.config_path.read_text(encoding="utf-8")
    assert db.get(CodexMcpSettings, 1) is None


def test_install_restores_config_when_database_commit_fails(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = service(db, tmp_path)
    configured.config_path.parent.mkdir()
    original = "# original\nmodel = \"keep\"\n"
    configured.config_path.write_text(original, encoding="utf-8")

    def fail_commit() -> None:
        raise CodexMcpSettingsError("database_failure", "forced failure")

    monkeypatch.setattr(configured, "_commit_settings", fail_commit)
    with pytest.raises(CodexMcpSettingsError) as exc_info:
        configured.install()

    assert exc_info.value.error_type == "database_failure"
    assert configured.config_path.read_text(encoding="utf-8") == original
    assert db.get(CodexMcpSettings, 1) is None
