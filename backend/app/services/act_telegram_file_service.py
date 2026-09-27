from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from app.services.act_workspace_service import ActWorkspace, ensure_act_workspace
from app.services.secret_store import SecretStore
from app.services.telegram_provider import (
    TELEGRAM_MAX_DOCUMENT_BYTES,
    TelegramBotApi,
    UrllibTelegramBotApi,
)
from app.services.telegram_service import TELEGRAM_ACT_ROLE, TelegramService


class ActTelegramFileError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(" ".join(message.split())[:256])
        self.error_type = error_type


class ActTelegramFileCapability:
    def __init__(
        self,
        db: Session,
        *,
        workspace: ActWorkspace | None = None,
        secret_store: SecretStore | None = None,
        api_factory: Callable[[str], TelegramBotApi] | None = None,
        telegram_service: TelegramService | None = None,
    ) -> None:
        self.workspace = workspace or ensure_act_workspace()
        self.telegram = telegram_service or TelegramService(
            db,
            role=TELEGRAM_ACT_ROLE,
            secret_store=secret_store,
            api_factory=api_factory or UrllibTelegramBotApi,
        )

    def send(self, session_id: int, path: str, caption: str | None = None) -> dict:
        source, display_path = self._workspace_file(path)
        if caption is not None and (not isinstance(caption, str) or not 1 <= len(caption) <= 1024):
            raise ActTelegramFileError(
                "invalid_input",
                "Telegram file caption must contain 1-1024 characters",
            )
        try:
            with source.open("rb") as handle:
                content = handle.read(TELEGRAM_MAX_DOCUMENT_BYTES + 1)
        except OSError:
            raise ActTelegramFileError("file_unavailable", "The workspace file could not be read") from None
        if not 1 <= len(content) <= TELEGRAM_MAX_DOCUMENT_BYTES:
            content = b""
            raise ActTelegramFileError(
                "file_size_unsupported",
                "The workspace file must contain 1 byte to 25 MiB",
            )
        message_id = self.telegram.send_act_workspace_document(
            session_id,
            filename=source.name,
            content=content,
            caption=caption,
        )
        size = len(content)
        content = b""
        return {
            "status": "sent",
            "path": display_path,
            "filename": source.name,
            "bytes": size,
            "message_id": message_id,
        }

    def _workspace_file(self, supplied_path: str) -> tuple[Path, str]:
        if not isinstance(supplied_path, str) or not supplied_path.strip() or len(supplied_path) > 1024:
            raise ActTelegramFileError("invalid_input", "A bounded workspace file path is required")
        raw = Path(supplied_path.strip())
        if raw.is_absolute():
            raise ActTelegramFileError("path_outside_workspace", "Only Act workspace files can be sent")
        try:
            workspace_root = self.workspace.workspace.resolve(strict=True)
            candidate = (self.workspace.root / raw).resolve(strict=True)
            candidate.relative_to(workspace_root)
        except (OSError, RuntimeError, ValueError):
            raise ActTelegramFileError("path_outside_workspace", "Only Act workspace files can be sent") from None
        if not candidate.is_file():
            raise ActTelegramFileError("file_unavailable", "The workspace path is not a regular file")
        display_path = candidate.relative_to(self.workspace.root.resolve()).as_posix()
        return candidate, display_path
