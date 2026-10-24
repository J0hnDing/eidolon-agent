from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ActLiveMessage, ActSession, ActTurn
from app.services.telegram_service import TELEGRAM_ACT_ROLE, TELEGRAM_ASSISTANT_ROLE, TelegramService


class ActChatMessageError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def accept_act_reply(db: Session, turn: ActTurn, text: str) -> bool:
    """Atomically attach one reply to a live prompt, without dispatching another turn."""
    now = datetime.now(UTC)
    prompt = db.scalar(select(ActLiveMessage).where(
        ActLiveMessage.turn_id == turn.id,
        ActLiveMessage.role == "assistant",
        ActLiveMessage.reply_status == "awaiting",
        ActLiveMessage.reply_expires_at > now,
    ).order_by(ActLiveMessage.id).limit(1))
    if prompt is None or turn.status != "running" or turn.cancel_requested_at is not None:
        return False
    claimed = db.execute(update(ActLiveMessage).where(
        ActLiveMessage.id == prompt.id,
        ActLiveMessage.reply_status == "awaiting",
        ActLiveMessage.reply_expires_at > now,
        ActLiveMessage.turn_id.in_(select(ActTurn.id).where(
            ActTurn.id == turn.id, ActTurn.status == "running", ActTurn.cancel_requested_at.is_(None),
        )),
    ).values(reply_status="answered"))
    if not claimed.rowcount:
        return False
    db.add(ActLiveMessage(
        turn_id=turn.id, role="user", text=text,
        reply_to_message_id=prompt.id, reply_status="none",
    ))
    db.flush()
    return True


def attach_live_messages(db: Session, turns: list[ActTurn]) -> None:
    grouped: dict[int, list[ActLiveMessage]] = {turn.id: [] for turn in turns}
    if grouped:
        for message in db.scalars(select(ActLiveMessage).where(
            ActLiveMessage.turn_id.in_(grouped),
        ).order_by(ActLiveMessage.id)):
            grouped[message.turn_id].append(message)
    for turn in turns:
        turn.live_messages = grouped[turn.id]


class ActChatMessageCapability:
    def __init__(self, db: Session, *, telegram_service: TelegramService | None = None) -> None:
        self.db = db
        self.telegram = telegram_service

    def send(
        self, session_id: int, text: str, wait_for_reply: bool = False, timeout_seconds: int = 120,
        *, agenda_question_id: int | None = None,
    ) -> dict:
        if (not isinstance(text, str) or not text.strip() or len(text) > 4000
                or not isinstance(wait_for_reply, bool)
                or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int)
                or not 1 <= timeout_seconds <= 120):
            raise ActChatMessageError("invalid_input", "A message of 1-4000 characters and a wait of 1-120 seconds are required")
        session = self.db.get(ActSession, session_id)
        turn = self.db.scalar(select(ActTurn).where(
            ActTurn.session_id == session_id, ActTurn.status == "running",
            ActTurn.cancel_requested_at.is_(None),
        ))
        if session is None or session.agent_id not in {"act", "assistant"} or session.status != "active" or turn is None:
            raise ActChatMessageError("act_turn_unavailable", "Messaging requires the current running Act turn")
        if turn.delivery_provider not in {None, "telegram"}:
            raise ActChatMessageError("unsupported_transport", "This Act conversation has no supported message transport")
        # Publish the prompt before sending remotely so a quick reply cannot be lost.
        message = ActLiveMessage(
            turn_id=turn.id, role="assistant", text=text,
            agenda_question_id=agenda_question_id,
            reply_status="awaiting" if wait_for_reply else "none",
            reply_expires_at=datetime.now(UTC) + timedelta(seconds=timeout_seconds) if wait_for_reply else None,
        )
        self.db.add(message)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ActChatMessageError("reply_wait_active", "This Act turn is already waiting for a reply") from None
        message_id, turn_id = message.id, turn.id
        from app.services.agent_memory_service import sync_chat_history_after_commit
        sync_chat_history_after_commit(self.db)
        telegram_message_id = None
        if turn.delivery_provider == "telegram":
            try:
                telegram = self.telegram or TelegramService(self.db,
                    role=TELEGRAM_ASSISTANT_ROLE if session.agent_id == "assistant" else TELEGRAM_ACT_ROLE)
                telegram_message_id = telegram.send_act_chat_message(turn, text)
            except Exception:
                self.db.rollback()
                self.db.execute(update(ActLiveMessage).where(
                    ActLiveMessage.id == message_id,
                ).values(reply_status="failed"))
                self.db.commit()
                raise
        result = {
            "status": "sent", "message_id": message_id,
            "telegram_message_id": telegram_message_id,
            "reply_status": "not_requested", "reply": None,
        }
        if wait_for_reply:
            result["reply_status"], result["reply"] = self._wait_for_reply(message_id, turn_id)
        return result

    def _wait_for_reply(self, message_id: int, turn_id: int) -> tuple[str, str | None]:
        while True:
            # Release read transactions between polls; replies arrive in another API/MCP process.
            self.db.rollback()
            self.db.expire_all()
            message = self.db.get(ActLiveMessage, message_id)
            turn = self.db.get(ActTurn, turn_id)
            if message is None:
                return "cancelled", None
            if turn is None or turn.status != "running" or turn.cancel_requested_at is not None:
                self._finish_wait(message_id, "cancelled")
                return "cancelled", None
            if message.reply_status == "answered":
                reply = self.db.scalar(select(ActLiveMessage.text).where(
                    ActLiveMessage.reply_to_message_id == message_id, ActLiveMessage.role == "user",
                ))
                return "received", reply
            if message.reply_status != "awaiting":
                return "cancelled", None
            expires = message.reply_expires_at.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                if self._finish_wait(message_id, "expired"):
                    return "timed_out", None
                continue  # A reply won the atomic claim at the deadline.
            self.db.rollback()
            time.sleep(0.2)

    def _finish_wait(self, message_id: int, status: str) -> bool:
        claimed = self.db.execute(update(ActLiveMessage).where(
            ActLiveMessage.id == message_id, ActLiveMessage.reply_status == "awaiting",
        ).values(reply_status=status))
        self.db.commit()
        return bool(claimed.rowcount)
