"""Transport-neutral delivery for completed Act/agent turns."""

from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    ActSession,
    ActTurn,
    IntegrationConnection,
    TelegramBotConnection,
    WeComObserverUserBinding,
)
from app.services.telegram_service import (
    TELEGRAM_AGENT_IDS,
    TELEGRAM_AGENT_LABELS,
    TelegramService,
)
from app.services.wecom_provider import WECOM_AGENT_ID, WeComProviderError
from app.services.wecom_service import wecom_observer_worker


def deliver_agent_turn_result(turn_id: int) -> None:
    """Deliver one pending result according to the turn's persisted transport."""
    db = SessionLocal()
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
        provider = turn.delivery_provider or "telegram"
        if provider == "telegram":
            _deliver_telegram(db, turn, session)
        elif provider == "wecom":
            _deliver_wecom(db, turn, session)
        else:
            _mark_failed(db, turn)
    except Exception:
        db.rollback()
        turn = db.get(ActTurn, turn_id)
        if turn is not None and turn.delivery_status == "pending":
            _mark_failed(db, turn)
    finally:
        db.close()


def _deliver_telegram(db, turn: ActTurn, session: ActSession | None) -> None:
    connection = db.get(TelegramBotConnection, turn.delivery_connection_id)
    if (
        connection is None
        or session is None
        or TELEGRAM_AGENT_IDS.get(connection.role) != session.agent_id
        or connection.status != "connected"
        or connection.paired_chat_id != turn.delivery_chat_id
    ):
        _mark_failed(db, turn)
        return
    agent_label = TELEGRAM_AGENT_LABELS.get(connection.role, _agent_label(session))
    TelegramService(db, role=connection.role).send_agent_turn_result(
        connection,
        _result_message(turn, agent_label),
    )
    turn.delivery_status = "delivered"
    db.commit()


def _deliver_wecom(db, turn: ActTurn, session: ActSession | None) -> None:
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
    ):
        _mark_failed(db, turn)
        return
    try:
        wecom_observer_worker.send_agent_turn_result(
            connection.id,
            turn.delivery_chat_id,
            _result_message(turn, "Observer"),
        )
    except WeComProviderError:
        raise
    turn.delivery_status = "delivered"
    db.commit()


def _result_message(turn: ActTurn, agent_label: str) -> str:
    if turn.status == "succeeded":
        return turn.assistant_message or f"{agent_label} completed without a text response."
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
