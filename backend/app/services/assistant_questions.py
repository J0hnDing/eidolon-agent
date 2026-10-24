"""Receipted agenda questions; timing and attention are Assistant judgments."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import ActLiveMessage, ActSession, ActTurn, AssistantAgendaItem, AssistantState
from app.services.act_chat_message_service import ActChatMessageCapability


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


class AssistantQuestionService:
    def __init__(self, db: Session):
        self.db = db

    def _receipts(self, state: AssistantState, now: datetime) -> list[ActLiveMessage]:
        zone = ZoneInfo(state.timezone)
        day = now.astimezone(zone).date()
        start = datetime.combine(day, time.min, zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
        return list(self.db.scalars(select(ActLiveMessage).where(
            ActLiveMessage.role == "assistant", ActLiveMessage.agenda_question_id.is_not(None),
            ActLiveMessage.created_at >= start, ActLiveMessage.created_at < end,
        ).order_by(ActLiveMessage.created_at, ActLiveMessage.id)))

    def status(self, state: AssistantState, now: datetime) -> dict:
        receipts = self._receipts(state, now)
        last_asked_at = self.db.scalar(select(ActLiveMessage.created_at).where(
            ActLiveMessage.role == "assistant", ActLiveMessage.agenda_question_id.is_not(None),
        ).order_by(ActLiveMessage.created_at.desc(), ActLiveMessage.id.desc()).limit(1))
        return {"asked_today": len(receipts), "timezone": state.timezone,
                "last_asked_at": last_asked_at}

    def ask(self, question_id: int, text: str, expected_revision: int, *, session_id: int, turn_id: int) -> dict:
        from app.services.agent_policy_service import AgentPolicyService
        from app.services.assistant_service import AssistantService

        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("Supply one question of 1-4000 characters")
        state = AssistantService(self.db).state()
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        self.db.refresh(state)
        AgentPolicyService(self.db).require_function("assistant", "chat.send_message")
        session = self.db.get(ActSession, session_id)
        turn = self.db.get(ActTurn, turn_id)
        if (not state.enabled or session is None or session.agent_id != "assistant" or session.status != "active"
                or turn is None or turn.session_id != session_id or turn.status != "running"
                or turn.cancel_requested_at is not None):
            raise ValueError("Question asking requires the current active Assistant turn")
        now = datetime.now(UTC)
        # A caller may have loaded the item before waiting for the state lock.
        item = self.db.get(AssistantAgendaItem, question_id, populate_existing=True)
        previous = self.db.scalar(select(ActLiveMessage).where(
            ActLiveMessage.role == "assistant", ActLiveMessage.agenda_question_id == question_id,
        ).order_by(ActLiveMessage.id.desc()).limit(1))
        if previous is not None and (item is None or item.status != "open"):
            self.db.commit()
            return {"status": "already_asked", "question_id": question_id, "message_id": previous.id}
        if (item is None or item.kind != "question" or item.status != "open"
                or item.revision != expected_revision):
            raise ValueError("Question changed or is no longer open; read the question list again")
        if item.due_at is not None and _utc(item.due_at) > now:
            raise ValueError("Question is scheduled for a future agenda time")
        # The chat message receipt and waiting state commit together
        # before any Telegram I/O. Repeated calls never send the same question twice.
        item.status, item.due_at, item.last_turn_id = "waiting", None, turn_id
        item.updated_at = now
        item.revision += 1
        result = ActChatMessageCapability(self.db).send(session_id, text, agenda_question_id=question_id)
        return {**result, "question_id": question_id,
                "asked_today": len(self._receipts(state, now))}
