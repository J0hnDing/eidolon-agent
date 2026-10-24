"""Transport-neutral delivery for completed Act/agent turns."""

from __future__ import annotations

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import (
    ActSession,
    ActTurn,
    IntegrationConnection,
    TelegramBotConnection,
    TelegramTopicSession,
    WeComObserverUserBinding,
)
from app.services.act_session_service import ActSessionError, ActSessionService
from app.services.agent_memory_service import AgentMemoryService
from app.services.telegram_service import (
    TELEGRAM_AGENT_IDS,
    TELEGRAM_AGENT_LABELS,
    TelegramService,
)
from app.services.wecom_provider import WECOM_AGENT_ID, WeComProviderError
from app.services.wecom_service import wecom_observer_worker


def deliver_agent_turn_result(turn_id: int, *, session_factory=SessionLocal) -> None:
    """Deliver one pending result according to the turn's persisted transport."""
    db = session_factory()
    turn: ActTurn | None = None
    try:
        turn = db.get(ActTurn, turn_id)
        if (
            turn is None
            or turn.delivery_status != "pending"
            or turn.delivery_connection_id is None
            or turn.delivery_chat_id is None
            or turn.status in {"queued", "running"}
        ):
            return
        session = db.get(ActSession, turn.session_id)
        content = AgentMemoryService(db).read_turn_content(turn, agent_id=session.agent_id if session else None)
        reply = content["assistant_message"]
        background_success = (session is not None and session.agent_id == "assistant"
                              and bool(turn.assistant_context_json) and turn.status == "succeeded")
        archived_silent = (background_success and (turn.assistant_review_json or {}).get(
            "chat_history", {}).get("has_assistant_response") is False)
        if turn.history_archived_at is not None and not content["available"] and not archived_silent:
            # Retention is authoritative even for a delivery missed during downtime.
            _mark_failed(db, turn)
            return
        if background_success and not (reply or "").strip():
            turn.delivery_status = "delivered"
            db.commit()
            return
        claimed = db.execute(update(ActTurn).where(
            ActTurn.id == turn.id, ActTurn.delivery_status == "pending",
        ).values(delivery_status="sending"))
        db.commit()
        if not claimed.rowcount:
            return
        provider = turn.delivery_provider or "telegram"
        if provider == "telegram":
            _deliver_telegram(db, turn, session, reply)
        elif provider == "wecom":
            _deliver_wecom(db, turn, session, reply)
        else:
            _mark_failed(db, turn)
    except Exception:
        db.rollback()
        turn = db.get(ActTurn, turn_id)
        if turn is not None and turn.delivery_status in {"pending", "sending"}:
            _mark_failed(db, turn)
    finally:
        db.close()


def _deliver_telegram(db, turn: ActTurn, session: ActSession | None, reply: str | None) -> None:
    connection = db.get(TelegramBotConnection, turn.delivery_connection_id)
    if (
        connection is None
        or session is None
        or TELEGRAM_AGENT_IDS.get(connection.role) != session.agent_id
        or connection.status != "connected"
        or not connection.paired_user_id
        or connection.paired_chat_id != turn.delivery_chat_id
        or session.status != "active"
        or (session.agent_id != "assistant" and turn.delivery_message_thread_id is None)
    ):
        _mark_failed(db, turn)
        return
    topic = None if session.agent_id == "assistant" else db.scalar(
        select(TelegramTopicSession).where(
            TelegramTopicSession.connection_id == connection.id,
            TelegramTopicSession.telegram_chat_id == turn.delivery_chat_id,
            TelegramTopicSession.message_thread_id == turn.delivery_message_thread_id,
            TelegramTopicSession.session_id == turn.session_id,
        )
    )
    if session.agent_id != "assistant" and topic is None:
        _mark_failed(db, turn)
        return
    agent_label = TELEGRAM_AGENT_LABELS.get(connection.role, _agent_label(session))
    TelegramService(db, role=connection.role).send_agent_turn_result(
        connection,
        _result_message(turn, agent_label, reply),
        message_thread_id=None if session.agent_id == "assistant" else turn.delivery_message_thread_id,
        draft_id=turn.id,
    )
    turn.delivery_status = "delivered"
    db.commit()


def _deliver_wecom(db, turn: ActTurn, session: ActSession | None, reply: str | None) -> None:
    connection = db.get(IntegrationConnection, turn.delivery_connection_id)
    binding = db.scalar(
        select(WeComObserverUserBinding).where(
            WeComObserverUserBinding.connection_id == turn.delivery_connection_id,
            WeComObserverUserBinding.paired_user_id == turn.delivery_chat_id,
        )
    )
    if (
        connection is None
        or connection.provider != "wecom"
        or session is None
        or session.agent_id != WECOM_AGENT_ID
        or binding is None
        or connection.status != "connected"
        or session.status != "active"
    ):
        _mark_failed(db, turn)
        return
    if binding.current_session_id != turn.session_id:
        try:
            ActSessionService(db, agent_id=WECOM_AGENT_ID).archive(session.id)
        except ActSessionError:
            db.rollback()
        turn = db.get(ActTurn, turn.id)
        if turn is not None:
            _mark_failed(db, turn)
        return
    try:
        wecom_observer_worker.send_agent_turn_result(
            connection.id,
            turn.delivery_chat_id,
            _result_message(turn, "Observer", reply),
        )
    except WeComProviderError:
        raise
    turn.delivery_status = "delivered"
    db.commit()


def _result_message(turn: ActTurn, agent_label: str, reply: str | None) -> str:
    if turn.status == "succeeded":
        return reply or f"{agent_label} completed without a text response."
    if turn.status == "cancelled":
        return f"{agent_label} turn #{turn.id} was cancelled."
    return f"{agent_label} turn #{turn.id} failed: {turn.error_message or turn.status}"


def _agent_label(session: ActSession) -> str:
    return {"act": "Act", "observer": "Observer", "assistant": "Assistant"}.get(
        session.agent_id,
        session.agent_id.title(),
    )


def _mark_failed(db, turn: ActTurn) -> None:
    turn.delivery_status = "failed"
    db.commit()
