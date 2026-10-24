"""Editable planning defaults and verifiable outcomes for ordinary agenda tasks."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update

from app.models import ActLiveMessage, ActSession, ActTurn, AssistantAgendaItem, AssistantState

LEGACY_MORNING_NOTES = (
    "Review current circumstances and plan the day. Call assistant.changes.list and follow its "
    "pagination to review backend-detected changes; retrieve authoritative details as needed. "
    "Review the agenda, advance authorized work, and schedule useful questions considerately. "
    "Leave unresolved responsibilities in the agenda. Communicate only useful results; an empty "
    "final response is appropriate when nothing needs the user's attention."
)

PREVIOUS_MORNING_NOTES = """Plan today and the coming days around your two jobs: understand the user and help the user. Call assistant.changes.list and follow its pagination, inspect the agenda and whole question list, and read relevant current Atlas details, commitments, todos and recent summaries.

Consider the person as well as their obligations: what deserves attention, practical help, conversation or personal follow-through? Save worthwhile intentions as agenda tasks and questions with suitable times or clear dependencies. Build on existing responsibilities and answers, and revise stale plans. Their agenda activations carry out the work and conversations.

Check current.daily_interaction and plan an opportunity for today's required interaction if needed. Review the saved agenda for useful next steps and timing, then finish silently."""

PREVIOUS_STATES_MORNING_NOTES = """Plan today and the coming days around your two jobs: understand the user and help the user. Call assistant.changes.list and follow its pagination, inspect the agenda and whole question list, and read relevant current Atlas details, commitments, todos and recent summaries. Read current Atlas States through atlas.states.get without an at timestamp, including individual goal, interest and relationship estimates, recency and confidence; read relevant configuration and history when needed to understand them.

Consider States alongside the user's wishes and circumstances when choosing useful help, conversation and follow-through; they do not dictate the plan. Treat stale progress or uncertain satisfaction as a reason to understand the user's current priorities, not proof of disinterest. Where useful, ask whether a goal or interest still matters or offer to find a free block for it. Ignore physical_activity and mental_stimulation for now; physical_energy and mental_energy remain relevant.

Save worthwhile intentions as agenda tasks and questions with suitable times or clear dependencies. Build on existing responsibilities and answers, and revise stale plans. Their agenda activations carry out the work and conversations.

Check current.daily_interaction and plan an opportunity for today's required interaction if needed. Review the saved agenda for useful next steps and timing, then finish silently."""

MORNING_NOTES = PREVIOUS_STATES_MORNING_NOTES.replace(
    "read relevant configuration and history when needed to understand them.",
    "read relevant configuration and history when needed to understand them. "
    "Goals and Interests jointly inform the existing goal_progression State; "
    "personal Relationships inform the existing close_connection State. "
    "Read their individual estimates and uncertainty rather than treating either summary as the whole picture.",
)

DAILY_INTERACTION_NOTES = """Ensure today's meaningful Assistant interaction. Check current.daily_interaction; if already satisfied, finish silently. Otherwise use your understanding and the current agenda to choose a personal check-in, a worthwhile question, company or a useful result. Ask agenda questions through assistant.question.ask, or send one brief natural final message. Fit the timing to the user's commitments and quiet hours. A user reply is not required; finish after there is actual user-visible contact."""


def _upgrade_morning_notes(db) -> None:
    """Exact-content upgrades also reach installations with the old migration receipt."""
    untouched = (
        AssistantAgendaItem.notes.in_((LEGACY_MORNING_NOTES, PREVIOUS_MORNING_NOTES, PREVIOUS_STATES_MORNING_NOTES)),
        AssistantAgendaItem.recurrence_json["frequency"].as_string().is_not(None),
        AssistantAgendaItem.kind == "task",
    )
    if db.scalar(select(AssistantAgendaItem.id).where(*untouched).limit(1)) is None:
        return
    db.execute(update(AssistantAgendaItem).where(*untouched).values(
        notes=MORNING_NOTES, revision=AssistantAgendaItem.revision + 1, updated_at=datetime.now(UTC)))
    db.commit()


def migrate_planning_contract(db, state: AssistantState) -> None:
    """Upgrade untouched defaults once; preserve customized routines and deletions."""
    if state.planning_contract_migrated:
        _upgrade_morning_notes(db)
        return
    db.execute(update(AssistantState).where(AssistantState.id == state.id).values(id=state.id))
    db.refresh(state)
    if state.planning_contract_migrated:
        _upgrade_morning_notes(db)
        return
    from app.services.assistant_recurrence import next_at, next_item_id

    now = datetime.now(UTC)
    for item in db.scalars(select(AssistantAgendaItem).where(
        or_(AssistantAgendaItem.notes.in_((LEGACY_MORNING_NOTES, PREVIOUS_MORNING_NOTES, PREVIOUS_STATES_MORNING_NOTES, MORNING_NOTES)),
            AssistantAgendaItem.title == "Morning planning"),
        AssistantAgendaItem.recurrence_json["frequency"].as_string().is_not(None),
        AssistantAgendaItem.kind == "task",
    )):
        if item.notes in (LEGACY_MORNING_NOTES, PREVIOUS_MORNING_NOTES, PREVIOUS_STATES_MORNING_NOTES, MORNING_NOTES):
            item.notes = MORNING_NOTES
        item.revision += 1
        item.updated_at = now
    recurrence = {"frequency": "daily", "time": "21:00", "timezone": state.timezone}
    contact = db.scalar(select(AssistantAgendaItem).where(
        AssistantAgendaItem.title == "Daily check-in", AssistantAgendaItem.kind == "task",
        AssistantAgendaItem.recurrence_json.is_not(None),
    ).order_by(AssistantAgendaItem.id).limit(1))
    if contact is None:
        db.add(AssistantAgendaItem(id=next_item_id(db), title="Daily check-in", kind="task",
            notes=DAILY_INTERACTION_NOTES, recurrence_json=recurrence, recurrence_start_at=now,
            due_at=next_at(recurrence, now), completion_requirement="interaction_today"))
    else:
        contact.completion_requirement = "interaction_today"
        contact.revision += 1
        contact.updated_at = now
    state.planning_contract_migrated = True
    db.commit()


def daily_interaction_status(db, timezone: str, now: datetime) -> dict:
    """Count actual Assistant output, including canonical archived message bodies."""
    from app.services.agent_memory_service import AgentMemoryService

    zone = ZoneInfo(timezone)
    day = now.astimezone(zone).date()
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    sessions = select(ActSession.id).where(ActSession.agent_id == "assistant")
    turns = list(db.scalars(select(ActTurn).where(
        ActTurn.session_id.in_(sessions), ActTurn.status == "succeeded",
        ActTurn.backend_message_kind.is_(None), ActTurn.completed_at >= start, ActTurn.completed_at < end,
    )))
    live = list(db.scalars(select(ActLiveMessage).join(ActTurn).where(
        ActTurn.session_id.in_(sessions), ActLiveMessage.role == "assistant",
        ActLiveMessage.created_at >= start, ActLiveMessage.created_at < end,
    )))
    memory = AgentMemoryService(db)
    content = {}

    def read(turn):
        if turn.id not in content:
            content[turn.id] = memory.read_turn_content(turn, agent_id="assistant")
        return content[turn.id]

    timestamps = [turn.completed_at for turn in turns if (read(turn)["assistant_message"] or "").strip()]
    timestamps += [message.created_at for message in live
                   if read(db.get(ActTurn, message.turn_id))["live_messages"].get(message.id, "").strip()]
    timestamps = [stamp.replace(tzinfo=stamp.tzinfo or UTC).astimezone(UTC) for stamp in timestamps]
    return {"day": day.isoformat(), "timezone": timezone, "required": 1,
            "count": len(timestamps), "satisfied": bool(timestamps),
            "last_interaction_at": max(timestamps) if timestamps else None}


def daily_interaction_error(db, turn: ActTurn, now: datetime) -> str | None:
    """Keep the daily-contact requirement independent of planning judgment."""
    activation = turn.assistant_context_json or {}
    occurrence = activation.get("occurrence")
    if not occurrence:
        return None
    requirement = activation.get("completion_requirement")
    if requirement == "interaction_today":
        state = db.get(AssistantState, 1)
        timezone = state.timezone if state else occurrence["timezone"]
        if not daily_interaction_status(db, timezone, now)["satisfied"]:
            return "Daily interaction is incomplete: produce a meaningful user-visible message or agenda question."
    return None
