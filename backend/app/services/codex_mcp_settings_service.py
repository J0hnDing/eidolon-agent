from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

import tomlkit
from sqlalchemy.orm import Session
from tomlkit.items import Table

from app.models import CodexMcpSettings
from app.schemas.codex_mcp import CodexMcpStatus
from app.services.mcp_function_service import McpFunctionService

SERVER_NAME = "eidolon"
MAX_STATUS_ERROR_LENGTH = 512


class CodexMcpSettingsError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(" ".join(message.split())[:MAX_STATUS_ERROR_LENGTH])
        self.error_type = error_type


@dataclass
class CodexMcpSettingsService:
    db: Session
    codex_home: Path | None = None
    backend_python: Path | None = None
    backend_directory: Path | None = None
    project_root: Path | None = None

    def __post_init__(self) -> None:
        configured_home = self.codex_home or Path(os.getenv("CODEX_HOME", Path.home() / ".codex"))
        self.codex_home = configured_home.expanduser().resolve()
        self.config_path = self.codex_home / "config.toml"
        self.backend_python = (self.backend_python or Path(sys.executable)).resolve()
        self.backend_directory = (
            self.backend_directory or Path(__file__).resolve().parents[2]
        ).resolve()

    def status(self) -> CodexMcpStatus:
        settings = self.db.get(CodexMcpSettings, 1)
        enabled = bool(settings and settings.enabled)
        registered = False
        config_matches = False
        error_type = settings.last_error_type if settings else None
        error = settings.last_error_message if settings else None
        try:
            document = self._load_document()
            entry = self._entry(document)
            registered = entry is not None
            if entry is not None:
                current_fingerprint = self._fingerprint(entry)
                config_matches = bool(
                    settings
                    and settings.config_fingerprint
                    and current_fingerprint == settings.config_fingerprint
                    and current_fingerprint == self._fingerprint(self._expected_entry())
                )
                if settings and settings.config_fingerprint and current_fingerprint != settings.config_fingerprint:
                    error_type = "config_conflict"
                    error = "The Eidolon Codex MCP entry changed outside Eidolon; repair and removal are blocked."
            elif enabled:
                error_type = "config_missing"
                error = "The registered Eidolon Codex MCP entry is missing."
        except CodexMcpSettingsError as exc:
            error_type = exc.error_type
            error = str(exc)

        try:
            catalog = McpFunctionService(self.db, project_root=self.project_root)
            tool_count = catalog.tool_count
            excluded_ids = catalog.excluded_ids
        except Exception:
            tool_count = 0
            excluded_ids = ["backend.codex.call"]
            if error is None:
                error_type = "catalog_unavailable"
                error = "The Eidolon function catalog is unavailable."
        return CodexMcpStatus(
            enabled=enabled,
            registered=registered,
            config_matches=config_matches,
            available_tool_count=tool_count,
            excluded_ids=excluded_ids,
            config_path=str(self.config_path),
            restart_required=enabled and config_matches,
            error_type=error_type,
            error=error,
        )

    def install(self) -> CodexMcpStatus:
        return self._write_registration("install")

    def repair(self) -> CodexMcpStatus:
        return self._write_registration("repair")

    def remove(self) -> CodexMcpStatus:
        settings = self.db.get(CodexMcpSettings, 1)
        if settings is None:
            settings = CodexMcpSettings(id=1, enabled=False)
            self.db.add(settings)
        settings.enabled = False
        settings.last_error_type = None
        settings.last_error_message = None
        self._commit_settings()

        original = self._config_snapshot()
        try:
            document = self._load_document()
            entry = self._entry(document)
            if entry is not None:
                if not settings.config_fingerprint or self._fingerprint(entry) != settings.config_fingerprint:
                    raise CodexMcpSettingsError(
                        "config_conflict",
                        "The Eidolon Codex MCP entry is not the entry installed by Eidolon; it was not removed.",
                    )
                servers = document.get("mcp_servers")
                assert isinstance(servers, Table)
                del servers[SERVER_NAME]
                self._atomic_write(tomlkit.dumps(document))
                if self._entry(self._load_document()) is not None:
                    raise CodexMcpSettingsError("verification_failed", "Codex MCP removal could not be verified.")
            settings.config_fingerprint = None
            self._commit_settings()
            return self.status()
        except Exception as exc:
            self._restore_config(original)
            error = exc if isinstance(exc, CodexMcpSettingsError) else CodexMcpSettingsError(
                "internal_failure", "Codex MCP removal failed safely."
            )
            self._record_error(settings, error)
            raise error from None

    def _write_registration(self, action: Literal["install", "repair"]) -> CodexMcpStatus:
        original = self._config_snapshot()
        settings = self.db.get(CodexMcpSettings, 1)
        settings_existed = settings is not None
        try:
            document = self._load_document()
            existing = self._entry(document)
            expected = self._expected_entry()
            expected_fingerprint = self._fingerprint(expected)
            if action == "install":
                if existing is not None:
                    if not settings or not settings.config_fingerprint:
                        raise CodexMcpSettingsError(
                            "config_conflict",
                            "A pre-existing Eidolon Codex MCP entry is not owned by Eidolon.",
                        )
                    current_fingerprint = self._fingerprint(existing)
                    if current_fingerprint != settings.config_fingerprint:
                        raise CodexMcpSettingsError(
                            "config_conflict",
                            "The Eidolon Codex MCP entry changed outside Eidolon.",
                        )
                    if current_fingerprint != expected_fingerprint:
                        raise CodexMcpSettingsError(
                            "repair_required",
                            "The owned Eidolon Codex MCP entry uses outdated paths; use Repair.",
                        )
            else:
                if settings is None or not settings.config_fingerprint:
                    raise CodexMcpSettingsError(
                        "not_owned",
                        "Eidolon has no owned Codex MCP registration to repair.",
                    )
                if existing is not None and self._fingerprint(existing) != settings.config_fingerprint:
                    raise CodexMcpSettingsError(
                        "config_conflict",
                        "The Eidolon Codex MCP entry changed outside Eidolon; repair is blocked.",
                    )

            self._set_entry(document, expected)
            self._atomic_write(tomlkit.dumps(document))
            verified = self._entry(self._load_document())
            if verified is None or self._fingerprint(verified) != expected_fingerprint:
                raise CodexMcpSettingsError("verification_failed", "Codex MCP installation could not be verified.")

            if settings is None:
                settings = CodexMcpSettings(id=1)
                self.db.add(settings)
            settings.enabled = True
            settings.config_fingerprint = expected_fingerprint
            settings.last_error_type = None
            settings.last_error_message = None
            self._commit_settings()
            return self.status()
        except Exception as exc:
            self.db.rollback()
            self._restore_config(original)
            error = exc if isinstance(exc, CodexMcpSettingsError) else CodexMcpSettingsError(
                "internal_failure", "Codex MCP registration failed safely."
            )
            if settings_existed:
                self.db.expire_all()
                persisted_settings = self.db.get(CodexMcpSettings, 1)
                if persisted_settings is not None:
                    self._record_error(persisted_settings, error)
            raise error from None

    def _expected_entry(self) -> dict[str, Any]:
        return {
            "command": str(self.backend_python),
            "args": ["-m", "app.mcp_server"],
            "cwd": str(self.backend_directory),
            "enabled": True,
            "required": False,
            "default_tools_approval_mode": "writes",
            "startup_timeout_sec": 15,
            "tool_timeout_sec": 180,
        }

    def _load_document(self):
        if not self.config_path.exists():
            return tomlkit.document()
        try:
            return tomlkit.parse(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomlkit.exceptions.TOMLKitError):
            raise CodexMcpSettingsError(
                "invalid_config", "The Codex configuration could not be read as valid UTF-8 TOML."
            ) from None

    @staticmethod
    def _entry(document) -> dict[str, Any] | None:
        servers = document.get("mcp_servers")
        if servers is None:
            return None
        if not isinstance(servers, Table):
            raise CodexMcpSettingsError("config_conflict", "The Codex mcp_servers value is not a TOML table.")
        item = servers.get(SERVER_NAME)
        if item is None:
            return None
        if not isinstance(item, Table):
            raise CodexMcpSettingsError("config_conflict", "The Eidolon Codex MCP entry is not a TOML table.")
        return item.unwrap()

    @staticmethod
    def _set_entry(document, entry: dict[str, Any]) -> None:
        servers = document.get("mcp_servers")
        if servers is None:
            servers = tomlkit.table()
            document.add("mcp_servers", servers)
        if not isinstance(servers, Table):
            raise CodexMcpSettingsError("config_conflict", "The Codex mcp_servers value is not a TOML table.")
        table = tomlkit.table()
        for key, value in entry.items():
            table.add(key, value)
        servers[SERVER_NAME] = table

    @staticmethod
    def _fingerprint(entry: dict[str, Any]) -> str:
        encoded = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _atomic_write(self, content: str) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        mode = self.config_path.stat().st_mode if self.config_path.exists() else None
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=self.config_path.parent,
                prefix=f".{self.config_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            if mode is not None:
                os.chmod(temporary_path, mode)
            os.replace(temporary_path, self.config_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def _config_snapshot(self) -> tuple[bool, bytes]:
        if not self.config_path.exists():
            return False, b""
        try:
            return True, self.config_path.read_bytes()
        except OSError:
            raise CodexMcpSettingsError("config_unavailable", "The Codex configuration could not be read.") from None

    def _restore_config(self, snapshot: tuple[bool, bytes]) -> None:
        existed, content = snapshot
        if existed:
            if self.config_path.exists() and self.config_path.read_bytes() == content:
                return
            self._atomic_write(content.decode("utf-8"))
        elif self.config_path.exists():
            self.config_path.unlink()

    def _commit_settings(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise CodexMcpSettingsError("database_failure", "Codex MCP settings could not be saved.") from None

    def _record_error(self, settings: CodexMcpSettings, error: CodexMcpSettingsError) -> None:
        try:
            settings.last_error_type = error.error_type
            settings.last_error_message = str(error)
            self.db.add(settings)
            self.db.commit()
        except Exception:
            self.db.rollback()
