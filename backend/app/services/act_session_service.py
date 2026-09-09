from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.models import (
    ActSession,
    ActTurn,
    AgentPolicy,
)
from app.services.act_app_server_service import (
    ActAppServerService,
    agent_app_servers,
    is_missing_rollout_error,
)
from app.services.agent_policy_service import AgentPolicyService

ACTIVE_TURN_STATUSES = ("queued", "running")


class ActSessionError(RuntimeError):
    pass


class AssistantSessionCapacityError(ActSessionError):
    pass


class ActSessionService:
    def __init__(
        self,
        db: Session,
        *,
        app_server: ActAppServerService | None = None,
        agent_id: str = "act",
    ) -> None:
        self.db = db
        AgentPolicyService.require_agent(agent_id)
        self.agent_id = agent_id
        self.app_server = app_server or agent_app_servers[agent_id]

    def list_sessions(self) -> list[ActSession]:
        return list(
            self.db.scalars(
                select(ActSession)
                .where(ActSession.status == "active", ActSession.agent_id == self.agent_id)
                .order_by(ActSession.updated_at.desc(), ActSession.id.desc())
            )
        )

    def read_session(self, session_id: int) -> ActSession:
        session = self.db.get(ActSession, session_id)
        if session is None or session.agent_id != self.agent_id:
            raise ActSessionError("Agent session was not found")
        session.turns = list(
            self.db.scalars(
                select(ActTurn).where(ActTurn.session_id == session.id).order_by(ActTurn.id)
            )
        )
        return session

    def create_session(self, *, origin: str = "web", commit: bool = True) -> ActSession:
        if self.agent_id == "assistant":
            self.prune_assistant_sessions(limit=4)
        session = ActSession(codex_thread_id="pending:" + uuid4().hex, origin=origin,
                             agent_id=self.agent_id, title=f"New {self.agent_id}")
        self.db.add(session)
        self.db.flush()
        if commit:
            self.db.commit()
            self.db.refresh(session)
        return session

    def prune_assistant_sessions(self, *, limit: int = 5) -> None:
        # A SQLite write claim serializes retention + creation across API/MCP
        # processes, not just Python threads. Preserve any existing policy.
        statement = insert(AgentPolicy).values(id="assistant", policy_json=AgentPolicyService(self.db).policy("assistant").model_dump(), revision=1)
        self.db.execute(statement.on_conflict_do_update(index_elements=[AgentPolicy.id], set_={"revision": AgentPolicy.revision}))
        sessions = list(self.db.scalars(select(ActSession).where(ActSession.agent_id == "assistant").order_by(ActSession.created_at, ActSession.id)))
        for old in sessions[:max(0, len(sessions) - limit)]:
            if self.db.scalar(select(ActTurn.id).where(ActTurn.session_id == old.id, ActTurn.status.in_(ACTIVE_TURN_STATUSES))):
                self.db.rollback()
                raise AssistantSessionCapacityError("Assistant session capacity is full because the oldest session is busy")
            if not old.codex_thread_id.startswith("pending:"):
                try:
                    self.app_server.sessions.archive_thread(old.codex_thread_id)
                except Exception as exc:
                    if not is_missing_rollout_error(exc):
                        self.db.rollback()
                        raise ActSessionError("Could not archive the oldest Assistant thread") from None
            AgentPolicyService(self.db).revoke(old.id)
            self.db.delete(old)
        self.db.flush()

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
        session = self.read_session(session_id)
        if session.status != "active":
            raise ActSessionError("Agent session is archived")
        active = self.db.scalar(
            select(ActTurn).where(
                ActTurn.session_id == session.id,
                ActTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        )
        if active is not None:
            raise ActSessionError("An agent turn is already queued or running for this session")
        turn = ActTurn(
            session_id=session.id,
            user_message=message,
            status="queued",
            activity_json=[{"kind": "queued", "label": f"Queued for {self.agent_id.title()}"}],
            # Null provider is retained only for pre-transport rows created by
            # older callers; all new remote turns identify their transport.
            delivery_provider=delivery_provider or ("telegram" if delivery_connection_id is not None else None),
            delivery_connection_id=delivery_connection_id,
            delivery_chat_id=delivery_chat_id,
            delivery_message_thread_id=delivery_message_thread_id,
            delivery_status="pending" if delivery_connection_id is not None else None,
        )
        if session.title == f"New {self.agent_id}":
            session.title = " ".join(message.split())[:160] or "New act"
        session.updated_at = datetime.now(UTC)
        self.db.add(turn)
        self.db.flush()
        if commit:
            self.db.commit()
            self.db.refresh(turn)
            from app.services.act_turn_dispatcher import agent_dispatchers
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
            return turn
        self.db.commit()
        if turn.codex_turn_id:
            try:
                self.app_server.sessions.interrupt_turn(session.codex_thread_id, turn.codex_turn_id)
            except Exception as exc:
                raise ActSessionError(f"Act cancellation could not be confirmed: {exc}") from None
        self.db.refresh(turn)
        return turn

    def archive(self, session_id: int, *, commit: bool = True) -> None:
        session = self.read_session(session_id)
        if self.db.scalar(
            select(ActTurn).where(
                ActTurn.session_id == session.id,
                ActTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        ):
            raise ActSessionError("Cannot archive an Act session while a turn is queued or running")
        try:
            if not session.codex_thread_id.startswith("pending:"):
                self.app_server.sessions.archive_thread(session.codex_thread_id)
        except Exception as exc:
            if not is_missing_rollout_error(exc):
                raise ActSessionError(f"Could not archive the Codex thread: {exc}") from None
        AgentPolicyService(self.db).revoke(session.id)
        session.status = "archived"
        session.updated_at = datetime.now(UTC)
        if commit:
            self.db.commit()
