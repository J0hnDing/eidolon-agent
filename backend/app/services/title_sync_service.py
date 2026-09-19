from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActSession, TelegramBotConnection, TelegramTopicSession
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store
from app.services.telegram_provider import TelegramBotApi, UrllibTelegramBotApi

logger = logging.getLogger(__name__)

SESSION_TITLE_MAX_CHARS = 160
TELEGRAM_TOPIC_TITLE_MAX_CHARS = 128
TELEGRAM_SECRET_NAMESPACE = "telegram"


class CodexThreadNameClient(Protocol):
    def read_thread_name(self, thread_id: str) -> str | None: ...

    def set_thread_name(self, thread_id: str, name: str) -> None: ...


def normalize_title(value: object, *, maximum: int = SESSION_TITLE_MAX_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:maximum]


class TitleSyncService:
    """Synchronize one session title across Codex, Eidolon, and Telegram."""

    def __init__(
        self,
        db: Session,
        *,
        telegram_secret_store: SecretStore | None = None,
        telegram_api_factory: Callable[[str], TelegramBotApi] = UrllibTelegramBotApi,
        codex_sessions_factory: Callable[[str], CodexThreadNameClient] | None = None,
    ) -> None:
        self.db = db
        self.telegram_secret_store = telegram_secret_store
        self.telegram_api_factory = telegram_api_factory
        self.codex_sessions_factory = codex_sessions_factory or _default_codex_sessions

    def sync_from_codex(self, session: ActSession, codex_sessions: CodexThreadNameClient) -> bool:
        """Read the native Codex name after a successful turn and project it."""

        if session.status != "active" or session.codex_thread_id.startswith("pending:"):
            return False
        try:
            native_name = codex_sessions.read_thread_name(session.codex_thread_id)
        except Exception as exc:  # noqa: BLE001 - title sync cannot change turn outcome.
            self._log_failure("Codex title read", session, exc)
            return False
        normalized = normalize_title(native_name)
        if not normalized:
            return False

        mapping = self._mapping_for_session(session.id)
        session_changed = normalize_title(session.title) != normalized
        topic_title = normalize_title(normalized, maximum=TELEGRAM_TOPIC_TITLE_MAX_CHARS)
        topic_changed = (
            mapping is not None
            and normalize_title(mapping.topic_name, maximum=TELEGRAM_TOPIC_TITLE_MAX_CHARS) != topic_title
        )
        if not session_changed and not topic_changed:
            return False

        if session_changed:
            session.title = normalized
            session.updated_at = datetime.now(UTC)
            self.db.commit()

        if mapping is None or not topic_changed:
            return session_changed

        try:
            self._edit_telegram_topic(mapping, topic_title)
        except Exception as exc:  # noqa: BLE001 - transport failure is best effort.
            self._log_failure("Telegram title update", session, exc)
            return session_changed

        mapping.topic_name = topic_title
        self.db.commit()
        return True

    def sync_from_telegram(
        self,
        session: ActSession,
        name: str,
        *,
        mapping: TelegramTopicSession | None = None,
        propagate_to_codex: bool = True,
    ) -> bool:
        """Persist a Telegram rename and best-effort update the Codex thread."""

        if session.status != "active":
            return False
        normalized = normalize_title(name, maximum=TELEGRAM_TOPIC_TITLE_MAX_CHARS)
        if not normalized:
            return False
        mapping = mapping or self._mapping_for_session(session.id)
        changed = self._apply_local_title(session, normalized, mapping)
        if changed:
            self.db.commit()

        if propagate_to_codex and not session.codex_thread_id.startswith("pending:"):
            self._propagate_to_codex(session, normalized)
        return changed

    def sync_from_telegram_identity(
        self,
        connection_id: int,
        chat_id: str,
        message_thread_id: int,
        name: str,
    ) -> bool:
        mapping = self.db.scalar(
            select(TelegramTopicSession).where(
                TelegramTopicSession.connection_id == connection_id,
                TelegramTopicSession.telegram_chat_id == chat_id,
                TelegramTopicSession.message_thread_id == message_thread_id,
            )
        )
        if mapping is None:
            return False
        session = self.db.get(ActSession, mapping.session_id)
        if session is None:
            return False
        return self.sync_from_telegram(session, name, mapping=mapping)

    def apply_local_telegram_title(
        self,
        session: ActSession,
        name: str,
        *,
        mapping: TelegramTopicSession | None = None,
        update_session: bool = True,
    ) -> bool:
        """Apply a known Telegram topic name without changing Codex."""

        normalized = normalize_title(name, maximum=TELEGRAM_TOPIC_TITLE_MAX_CHARS)
        if not normalized:
            return False
        return self._apply_local_title(session, normalized, mapping, update_session=update_session)

    def _apply_local_title(
        self,
        session: ActSession,
        normalized: str,
        mapping: TelegramTopicSession | None,
        *,
        update_session: bool = True,
    ) -> bool:
        changed = False
        if update_session and normalize_title(session.title) != normalized:
            session.title = normalized
            session.updated_at = datetime.now(UTC)
            changed = True
        if mapping is not None and normalize_title(
            mapping.topic_name,
            maximum=TELEGRAM_TOPIC_TITLE_MAX_CHARS,
        ) != normalized:
            mapping.topic_name = normalized
            changed = True
        return changed

    def _propagate_to_codex(self, session: ActSession, normalized: str) -> None:
        try:
            codex_sessions = self.codex_sessions_factory(session.agent_id)
            current_name = codex_sessions.read_thread_name(session.codex_thread_id)
            if normalize_title(current_name) == normalized:
                return
            codex_sessions.set_thread_name(session.codex_thread_id, normalized)
        except Exception as exc:  # noqa: BLE001 - manual rename remains persisted locally.
            self._log_failure("Codex title update", session, exc)

    def _edit_telegram_topic(self, mapping: TelegramTopicSession, name: str) -> None:
        connection = self.db.get(TelegramBotConnection, mapping.connection_id)
        if connection is None or connection.status != "connected":
            raise RuntimeError("Telegram connection is unavailable")
        if connection.paired_chat_id != mapping.telegram_chat_id:
            raise RuntimeError("Telegram topic binding is stale")

        secret_store = self.telegram_secret_store
        if secret_store is None:
            secret_store = default_secret_store()
        if secret_store.implementation_id != connection.secret_store_id:
            raise SecretStoreError("Telegram secret storage is unavailable")
        token = secret_store.get(connection.secret_reference, namespace=TELEGRAM_SECRET_NAMESPACE)
        try:
            self.telegram_api_factory(token).edit_forum_topic(
                int(mapping.telegram_chat_id),
                mapping.message_thread_id,
                name=name,
            )
        finally:
            token = ""

    @staticmethod
    def _log_failure(operation: str, session: ActSession, exc: BaseException) -> None:
        logger.warning(
            "%s failed for agent session %s: %s",
            operation,
            session.id,
            type(exc).__name__,
        )

    def _mapping_for_session(self, session_id: int) -> TelegramTopicSession | None:
        return self.db.scalar(
            select(TelegramTopicSession).where(TelegramTopicSession.session_id == session_id)
        )


def _default_codex_sessions(agent_id: str) -> CodexThreadNameClient:
    from app.services.act_app_server_service import agent_app_servers

    return agent_app_servers[agent_id].sessions
