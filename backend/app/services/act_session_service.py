from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActSession, ActTurn
from app.services.act_app_server_service import (
    ActAppServerError,
    ActAppServerService,
    act_app_server_service,
    is_missing_rollout_error,
)
from app.services.codex_routing_service import CodexRoutingService

ACTIVE_TURN_STATUSES = ("queued", "running")


class ActSessionError(RuntimeError):
    pass


class ActSessionService:
    def __init__(
        self,
        db: Session,
        *,
        app_server: ActAppServerService | None = None,
    ) -> None:
        self.db = db
        self.app_server = app_server or act_app_server_service

    def list_sessions(self) -> list[ActSession]:
        return list(
            self.db.scalars(
                select(ActSession)
                .where(ActSession.status == "active")
                .order_by(ActSession.updated_at.desc(), ActSession.id.desc())
            )
        )

    def read_session(self, session_id: int) -> ActSession:
        session = self.db.get(ActSession, session_id)
        if session is None:
            raise ActSessionError("Act session was not found")
        session.turns = list(
            self.db.scalars(
                select(ActTurn).where(ActTurn.session_id == session.id).order_by(ActTurn.id)
            )
        )
        return session

    def create_session(self, *, origin: str = "web") -> ActSession:
        try:
            routing = CodexRoutingService(self.db).resolve(role="act", action="act")
            thread_id = self.app_server.start_thread(
                self.db,
                model=routing.effective_model,
                reasoning_effort=routing.effective_reasoning_effort,
            )
        except Exception as exc:
            message = str(exc) if isinstance(exc, ActAppServerError) else f"Could not start Act: {exc}"
            raise ActSessionError(message) from None
        session = ActSession(codex_thread_id=thread_id, origin=origin)
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def enqueue_turn(
        self,
        session_id: int,
        message: str,
        *,
        delivery_connection_id: int | None = None,
        delivery_chat_id: str | None = None,
    ) -> ActTurn:
        session = self.read_session(session_id)
        if session.status != "active":
            raise ActSessionError("Act session is archived")
        active = self.db.scalar(
            select(ActTurn).where(
                ActTurn.session_id == session.id,
                ActTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        )
        if active is not None:
            raise ActSessionError("An Act turn is already queued or running for this session")
        turn = ActTurn(
            session_id=session.id,
            user_message=message,
            status="queued",
            activity_json=[{"kind": "queued", "label": "Queued for Act"}],
            delivery_connection_id=delivery_connection_id,
            delivery_chat_id=delivery_chat_id,
            delivery_status="pending" if delivery_connection_id is not None else None,
        )
        if session.title == "New act":
            session.title = " ".join(message.split())[:160] or "New act"
        session.updated_at = datetime.now(UTC)
        self.db.add(turn)
        self.db.commit()
        self.db.refresh(turn)
        from app.services.act_turn_dispatcher import act_turn_dispatcher

        act_turn_dispatcher.notify()
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
            return turn
        self.db.commit()
        if turn.codex_turn_id:
            try:
                self.app_server.sessions.interrupt_turn(session.codex_thread_id, turn.codex_turn_id)
            except Exception as exc:
                raise ActSessionError(f"Act cancellation could not be confirmed: {exc}") from None
        self.db.refresh(turn)
        return turn

    def archive(self, session_id: int) -> None:
        session = self.read_session(session_id)
        if self.db.scalar(
            select(ActTurn).where(
                ActTurn.session_id == session.id,
                ActTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        ):
            raise ActSessionError("Cannot archive an Act session while a turn is queued or running")
        try:
            self.app_server.sessions.archive_thread(session.codex_thread_id)
        except Exception as exc:
            if not is_missing_rollout_error(exc):
                raise ActSessionError(f"Could not archive the Codex thread: {exc}") from None
        session.status = "archived"
        session.updated_at = datetime.now(UTC)
        self.db.commit()
