from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    ActSession,
    ActTurn,
    TelegramBotConnection,
    WeComObserverUserBinding,
)
from app.services.act_app_server_service import (
    ActAppServerService,
    get_agent_app_server,
    is_missing_rollout_error,
    stop_act_app_server,
)
from app.services.agent_policy_service import AgentPolicyService

ACTIVE_TURN_STATUSES = ("queued", "running")


class ActSessionError(RuntimeError):
    pass



class ActSessionService:
    def __init__(
        self,
        db: Session,
        *,
        app_server: ActAppServerService | None = None,
        agent_id: str = "act",
        telegram_sync: Callable[[Session, ActSession], None] | None = None,
    ) -> None:
        self.db = db
        AgentPolicyService.require_agent(agent_id)
        self.agent_id = agent_id
        self.app_server = app_server
        self.telegram_sync = telegram_sync or _synchronize_session_to_telegram

    def list_sessions(self) -> list[ActSession]:
        if self.agent_id == "assistant":
            from app.services.assistant_service import AssistantService

            return [AssistantService(self.db).primary_session()]
        sessions = list(
            self.db.scalars(
                select(ActSession)
                .where(ActSession.status == "active", ActSession.agent_id == self.agent_id)
                .order_by(ActSession.updated_at.desc(), ActSession.id.desc())
            )
        )
        self._attach_wecom_users(sessions)
        return sessions

    def read_session(self, session_id: int) -> ActSession:
        session = self.db.get(ActSession, session_id)
        if session is None or session.agent_id != self.agent_id:
            raise ActSessionError("Agent session was not found")
        session.turns = list(
            self.db.scalars(
                select(ActTurn).where(ActTurn.session_id == session.id).order_by(ActTurn.id)
            )
        )
        from app.services.act_chat_message_service import attach_live_messages
        attach_live_messages(self.db, session.turns)
        self._attach_wecom_users([session])
        return session

    def create_session(self, *, origin: str = "web", commit: bool = True) -> ActSession:
        if self.agent_id == "assistant":
            from app.services.assistant_service import AssistantService

            session = AssistantService(self.db).primary_session(origin=origin, commit=False)
        else:
            session = self._new_session(origin=origin)
        if commit:
            self.db.commit()
            self.db.refresh(session)
            self.synchronize_telegram(session)
        return session

    def synchronize_telegram(self, session: ActSession) -> None:
        self.telegram_sync(self.db, session)

    def enqueue_turn(
        self,
        session_id: int,
        message: str,
        *,
        delivery_provider: str | None = None,
        delivery_connection_id: int | None = None,
        delivery_chat_id: str | None = None,
        delivery_message_thread_id: int | None = None,
        commit: bool = True,
    ) -> ActTurn:
        if not isinstance(message, str) or not message.strip() or len(message) > 16000:
            raise ActSessionError("A message must contain 1-16000 characters")
        session = self.read_session(session_id)
        if session.status != "active":
            raise ActSessionError("Agent session is archived")
        if self.agent_id == "assistant" and message.strip().casefold() == "/refresh":
            from app.services.assistant_service import AssistantService

            # This control message never becomes model input or a live reply.
            AssistantService(self.db).request_refresh(commit=False)
            turn = ActTurn(
                session_id=session.id, user_message=message, status="succeeded",
                backend_message_kind="assistant_refresh",
                assistant_message="Assistant session will refresh before the next turn.",
                activity_json=[], completed_at=datetime.now(UTC),
                delivery_provider=delivery_provider or ("telegram" if delivery_connection_id is not None else None),
                delivery_connection_id=delivery_connection_id, delivery_chat_id=delivery_chat_id,
                delivery_message_thread_id=delivery_message_thread_id,
                delivery_status="pending" if delivery_connection_id is not None else None,
            )
            self.db.add(turn)
            self.db.flush()
            if commit:
                self.db.commit()
                from app.services.agent_memory_service import sync_chat_history_after_commit
                sync_chat_history_after_commit(self.db)
                if turn.delivery_status == "pending":
                    from app.services.agent_turn_delivery import deliver_agent_turn_result

                    deliver_agent_turn_result(turn.id)
            return turn
        active = self.db.scalar(
            select(ActTurn).where(
                ActTurn.session_id == session.id,
                ActTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        )
        if active is not None:
            if self.agent_id in {"act", "assistant"}:
                from app.services.act_chat_message_service import accept_act_reply, attach_live_messages
                if accept_act_reply(self.db, active, message):
                    session.updated_at = datetime.now(UTC)
                    if commit:
                        self.db.commit()
                        from app.services.agent_memory_service import sync_chat_history_after_commit
                        sync_chat_history_after_commit(self.db)
                    attach_live_messages(self.db, [active])
                    return active
            if self.agent_id != "assistant":
                raise ActSessionError("An agent turn is already queued or running for this session")
        bound_connection_id, bound_chat_id = delivery_connection_id, delivery_chat_id
        if self.agent_id == "assistant" and delivery_connection_id is None:
            # An explicit telegram.send_file call from web Chat uses this fixed
            # pairing snapshot. Leave provider/status null so ordinary web answers
            # do not become automatic Telegram deliveries.
            connection = self.db.scalar(select(TelegramBotConnection).where(
                TelegramBotConnection.role == "assistant_agent",
                TelegramBotConnection.is_default.is_(True),
                TelegramBotConnection.status == "connected",
                TelegramBotConnection.paired_chat_id.is_not(None),
                TelegramBotConnection.paired_user_id.is_not(None),
            ).order_by(TelegramBotConnection.id.desc()).limit(1))
            if connection is not None:
                bound_connection_id, bound_chat_id = connection.id, connection.paired_chat_id
        turn = ActTurn(
            session_id=session.id,
            user_message=message,
            status="queued",
            activity_json=[{"kind": "queued", "label": f"Queued for {self.agent_id.title()}"}],
            # Remote turns identify their transport. Web Assistant turns may
            # retain a binding snapshot without automatic remote delivery.
            delivery_provider=delivery_provider or ("telegram" if delivery_connection_id is not None else None),
            delivery_connection_id=bound_connection_id,
            delivery_chat_id=bound_chat_id,
            delivery_message_thread_id=delivery_message_thread_id,
            delivery_status="pending" if delivery_connection_id is not None else None,
        )
        session.updated_at = datetime.now(UTC)
        self.db.add(turn)
        self.db.flush()
        if commit:
            self.db.commit()
            self.db.refresh(turn)
            from app.services.agent_memory_service import sync_chat_history_after_commit
            sync_chat_history_after_commit(self.db)
            from app.services.agent_dispatchers import agent_dispatchers
            agent_dispatchers[self.agent_id].notify()
        return turn

    def cancel_turn(self, session_id: int, turn_id: int) -> ActTurn:
        session = self.read_session(session_id)
        turn = self.db.get(ActTurn, turn_id)
        if turn is None or turn.session_id != session.id:
            raise ActSessionError("Act turn was not found")
        if turn.status not in ACTIVE_TURN_STATUSES:
            raise ActSessionError("Act turn is already complete")
        turn.cancel_requested_at = datetime.now(UTC)
        if turn.status == "queued":
            turn.status = "cancelled"
            turn.completed_at = datetime.now(UTC)
            turn.activity_json = [{"kind": "cancelled", "label": "Cancelled before Act started"}]
            self.db.commit()
            self.db.refresh(turn)
            from app.services.agent_proposal_service import AgentProposalService

            AgentProposalService(self.db).refresh_execution(session.id)
            if self.agent_id == "assistant":
                from app.services.assistant_activation_service import AssistantActivationService
                AssistantActivationService(self.db).on_turn_completed(turn)
            from app.services.agent_memory_service import sync_chat_history_after_commit
            sync_chat_history_after_commit(self.db)
            return turn
        self.db.commit()
        if turn.codex_turn_id:
            try:
                self._app_server_for(session).sessions.interrupt_turn(session.codex_thread_id, turn.codex_turn_id)
            except Exception as exc:
                raise ActSessionError(f"Act cancellation could not be confirmed: {exc}") from None
        self.db.refresh(turn)
        return turn

    def archive(self, session_id: int, *, commit: bool = True) -> None:
        session = self.read_session(session_id)
        if self.agent_id == "assistant":
            raise ActSessionError("The Assistant has one permanent chat. Use /refresh to start a fresh session.")
        if self.db.scalar(
            select(ActTurn).where(
                ActTurn.session_id == session.id,
                ActTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        ):
            raise ActSessionError("Cannot archive an Act session while a turn is queued or running")
        wecom_bindings = (
            list(
                self.db.scalars(
                    select(WeComObserverUserBinding).where(
                        WeComObserverUserBinding.current_session_id == session.id
                    )
                )
            )
            if self.agent_id == "observer"
            else []
        )
        for binding in wecom_bindings:
            replacement = self._new_session(origin="wecom")
            claimed = self.db.execute(
                update(WeComObserverUserBinding)
                .where(
                    WeComObserverUserBinding.id == binding.id,
                    WeComObserverUserBinding.current_session_id == session.id,
                )
                .values(current_session_id=replacement.id, updated_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                self.db.delete(replacement)
                self.db.flush()
        try:
            if not session.codex_thread_id.startswith("pending:"):
                self._app_server_for(session).sessions.archive_thread(session.codex_thread_id)
        except Exception as exc:
            if not is_missing_rollout_error(exc):
                raise ActSessionError(f"Could not archive the Codex thread: {exc}") from None
        AgentPolicyService(self.db).revoke(session.id)
        session.status = "archived"
        session.updated_at = datetime.now(UTC)
        if commit:
            self.db.commit()
            from app.services.agent_memory_service import sync_chat_history_after_commit
            sync_chat_history_after_commit(self.db)
        if self.agent_id == "act" and self.app_server is None:
            stop_act_app_server(session.id)

    def _app_server_for(self, session: ActSession) -> ActAppServerService:
        return self.app_server or get_agent_app_server(self.agent_id, session.id)

    def _new_session(self, *, origin: str) -> ActSession:
        session = ActSession(
            codex_thread_id="pending:" + uuid4().hex,
            origin=origin,
            agent_id=self.agent_id,
            title="New Chat",
        )
        self.db.add(session)
        self.db.flush()
        return session

    def _attach_wecom_users(self, sessions: list[ActSession]) -> None:
        session_ids = [session.id for session in sessions]
        if not session_ids:
            return
        bindings = self.db.scalars(
            select(WeComObserverUserBinding).where(
                WeComObserverUserBinding.current_session_id.in_(session_ids)
            )
        )
        users_by_session = {
            binding.current_session_id: binding.paired_user_id
            for binding in bindings
            if binding.current_session_id is not None
        }
        unresolved_wecom_ids = [
            session.id
            for session in sessions
            if session.origin == "wecom" and session.id not in users_by_session
        ]
        if unresolved_wecom_ids:
            delivered_users = self.db.execute(
                select(ActTurn.session_id, ActTurn.delivery_chat_id)
                .where(
                    ActTurn.session_id.in_(unresolved_wecom_ids),
                    ActTurn.delivery_provider == "wecom",
                    ActTurn.delivery_chat_id.is_not(None),
                )
                .order_by(ActTurn.id.desc())
            )
            for session_id, user_id in delivered_users:
                if user_id is not None:
                    users_by_session.setdefault(session_id, user_id)
        for session in sessions:
            session.wecom_user_id = users_by_session.get(session.id)


def _synchronize_session_to_telegram(db: Session, session: ActSession) -> None:
    from app.services.telegram_service import synchronize_session_to_telegram

    synchronize_session_to_telegram(db, session)
