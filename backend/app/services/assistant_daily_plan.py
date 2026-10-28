"""Deliberate daily agenda selections and evidence of their follow-through.

The agenda remains the source of responsibilities. A selection receipt on the
planning turn preserves membership when activation or completion clears due_at.
Classification and the usefulness of actual work remain Assistant judgments.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select, update

from app.models import ActLiveMessage, ActSession, ActTurn, AssistantAgendaItem, AssistantState

PLAN_REQUIREMENTS = {"work": 2, "interaction": 2, "other": 1}


def has_activation_context(turn):
    """A plan receipt alone does not turn a user message into a background activation."""
    return bool(set(turn.assistant_context_json or {}) - {"daily_plan"})


def _utc(value):
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _bounds(timezone, now):
    zone = ZoneInfo(timezone)
    day = _utc(now).astimezone(zone).date()
    return (day.isoformat(), datetime.combine(day, time.min, zone).astimezone(UTC),
            datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC))


class _DayEvidence:
    def __init__(self, db, timezone, now):
        self.db = db
        self.now = now
        self.day, self.start, self.end = _bounds(timezone, now)
        sessions = select(ActSession.id).where(ActSession.agent_id == "assistant")
        self.turns = list(db.scalars(select(ActTurn).where(
            ActTurn.session_id.in_(sessions), ActTurn.backend_message_kind.is_(None),
            or_(and_(ActTurn.created_at >= self.start, ActTurn.created_at < self.end),
                and_(ActTurn.completed_at >= self.start, ActTurn.completed_at < self.end)),
        ).order_by(ActTurn.id)))
        self.questions = {}
        for message in db.scalars(select(ActLiveMessage).join(ActTurn).where(
            ActTurn.session_id.in_(sessions), ActLiveMessage.role == "assistant",
            ActLiveMessage.agenda_question_id.is_not(None),
            ActLiveMessage.created_at >= self.start, ActLiveMessage.created_at < self.end,
        ).order_by(ActLiveMessage.id)):
            self.questions[message.agenda_question_id] = message.created_at
        self.activations = {}
        self.content = {}
        for turn in self.turns:
            activation = turn.assistant_context_json or {}
            # A recurring briefing or other routine cannot perform selected work.
            if activation.get("occurrence") or turn.status not in {"queued", "running", "succeeded"}:
                continue
            for snapshot in activation.get("agenda", []):
                self.activations.setdefault(snapshot["id"], []).append(turn)

    def today(self, value):
        return value is not None and self.start <= _utc(value) < self.end

    def succeeded(self, item_id):
        return [turn for turn in self.activations.get(item_id, [])
                if turn.status == "succeeded" and self.today(turn.completed_at)]

    def output(self, turn, *, current=False, include_questions=False):
        from app.services.agent_memory_service import AgentMemoryService

        if turn.id not in self.content:
            self.content[turn.id] = AgentMemoryService(self.db).read_turn_content(turn, agent_id="assistant")
        content = self.content[turn.id]
        if ((current or turn.status == "succeeded" or turn.delivery_status == "sent")
                and self.today(turn.completed_at or (self.now if current else None))
                and (content["assistant_message"] or "").strip()):
            return True
        messages = select(ActLiveMessage).where(
            ActLiveMessage.turn_id == turn.id, ActLiveMessage.role == "assistant",
            ActLiveMessage.created_at >= self.start, ActLiveMessage.created_at < self.end)
        if not include_questions:
            messages = messages.where(ActLiveMessage.agenda_question_id.is_(None))
        return any(content["live_messages"].get(message.id, "").strip()
                   for message in self.db.scalars(messages))

    def scheduled(self, item):
        if item is None or item.status == "dismissed" or item.recurrence_json:
            return None
        if item.kind == "question" and item.status == "waiting" and item.id not in self.questions:
            return None
        if item.kind == "task" and item.status == "waiting" and not self.succeeded(item.id):
            return None
        if item.due_at is not None:
            return _utc(item.due_at) if self.today(item.due_at) else None
        if item.id in self.questions:
            return _utc(self.questions[item.id])
        for turn in reversed(self.activations.get(item.id, [])):
            if item.status == "done" and turn not in self.succeeded(item.id):
                continue
            instant = turn.completed_at if item.status == "done" else turn.created_at
            if self.today(instant):
                return _utc(instant)
        return None


def save_daily_plan(db, payload, *, session_id, turn_id, now=None):
    """Save a validated, disjoint selection of today's ordinary agenda items."""
    if not isinstance(payload, dict) or set(payload) != set(PLAN_REQUIREMENTS):
        raise ValueError("Supply work, interaction and other agenda ID lists")
    ids = []
    for category, minimum in PLAN_REQUIREMENTS.items():
        selected = payload[category]
        if (not isinstance(selected, list) or len(selected) < minimum
                or any(type(item_id) is not int or item_id < 1 for item_id in selected)):
            raise ValueError(f"Select at least {minimum} valid {category} agenda IDs")
        ids.extend(selected)
    if len(ids) > 30 or len(set(ids)) != len(ids):
        raise ValueError("Select at most 30 distinct agenda items; an item counts in only one category")
    db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
    state = db.get(AssistantState, 1, populate_existing=True)
    session = db.get(ActSession, session_id, populate_existing=True)
    turn = db.get(ActTurn, turn_id, populate_existing=True) if turn_id is not None else None
    if (state is None or not state.enabled or session is None or session.agent_id != "assistant"
            or session.status != "active" or turn is None or turn.session_id != session_id
            or turn.status != "running" or turn.cancel_requested_at is not None):
        raise ValueError("Saving a daily plan requires the current active running Assistant turn")
    now = now or datetime.now(UTC)
    evidence = _DayEvidence(db, state.timezone, now)
    items = {item.id: item for item in db.scalars(select(AssistantAgendaItem).where(
        AssistantAgendaItem.id.in_(ids)).execution_options(populate_existing=True))}
    selection = {}
    for category, selected in payload.items():
        selection[category] = []
        for item_id in selected:
            item = items.get(item_id)
            scheduled = evidence.scheduled(item)
            if (scheduled is None or category == "work" and item.kind != "task"
                    or item.status == "done" and not evidence.succeeded(item_id)
                    and item_id not in evidence.questions):
                raise ValueError(f"Agenda item {item_id} must be a nonrecurring {category} responsibility "
                                 "scheduled or actually activated/delivered today; read and revise it first")
            selection[category].append({"id": item.id, "title": item.title, "kind": item.kind,
                                        "revision": item.revision, "scheduled_at": scheduled.isoformat()})
    turn.assistant_context_json = {**(turn.assistant_context_json or {}), "daily_plan": {
        "day": evidence.day, "timezone": state.timezone, "saved_at": _utc(now).isoformat(), "items": selection,
    }}
    db.commit()
    return {"status": "saved", **daily_plan_status(db, state.timezone, now)}


def daily_plan_status(db, timezone, now):
    """Return selected responsibilities and observable completion/delivery evidence."""
    day, _, _ = _bounds(timezone, now)
    turn = db.scalar(select(ActTurn).join(ActSession).where(
        ActSession.agent_id == "assistant",
        ActTurn.assistant_context_json["daily_plan"]["day"].as_string() == day,
        ActTurn.assistant_context_json["daily_plan"]["timezone"].as_string() == timezone,
    ).order_by(ActTurn.id.desc()).limit(1))
    result = {"day": day, "timezone": timezone, "required": dict(PLAN_REQUIREMENTS),
              "planned": dict.fromkeys(PLAN_REQUIREMENTS, 0), "completed": dict.fromkeys(PLAN_REQUIREMENTS, 0),
              "planned_satisfied": False, "follow_through_satisfied": False,
              "plan_turn_id": turn.id if turn else None, "items": {category: [] for category in PLAN_REQUIREMENTS}}
    if turn is None:
        return result
    evidence = _DayEvidence(db, timezone, now)
    receipt = turn.assistant_context_json["daily_plan"]
    seen = set()
    used_interaction_turns = set()
    for category in PLAN_REQUIREMENTS:
        for snapshot in receipt.get("items", {}).get(category, []):
            item_id = snapshot["id"]
            if item_id in seen:
                continue
            seen.add(item_id)
            item = db.get(AssistantAgendaItem, item_id)
            scheduled = evidence.scheduled(item)
            valid = scheduled is not None and (category != "work" or item.kind == "task")
            succeeded = evidence.succeeded(item_id)
            completed = False
            if valid and category == "interaction":
                if item.kind == "question":
                    completed = item_id in evidence.questions
                elif item.status == "done":
                    delivery = next((attempt for attempt in succeeded
                                     if attempt.id not in used_interaction_turns and evidence.output(attempt)), None)
                    if delivery is not None:
                        used_interaction_turns.add(delivery.id)
                        completed = True
            elif valid:
                completed = item.status == "done" and bool(succeeded)
            result["planned"][category] += int(valid)
            result["completed"][category] += int(completed)
            result["items"][category].append({
                "id": item_id, "title": item.title if item else snapshot["title"],
                "kind": item.kind if item else snapshot["kind"], "status": item.status if item else "deleted",
                "due_at": _utc(item.due_at).isoformat() if item and item.due_at else None,
                "scheduled_at": snapshot["scheduled_at"], "planned": valid, "completed": completed,
                "progress": "completed" if completed else "needs_replanning" if not valid
                else "waiting" if item.status == "waiting" else "pending",
            })
    result["planned_satisfied"] = all(result["planned"][key] >= count for key, count in PLAN_REQUIREMENTS.items())
    result["follow_through_satisfied"] = all(result["completed"][key] >= count for key, count in PLAN_REQUIREMENTS.items())
    return result


def morning_plan_error(db, turn, now):
    """Enforce the saved 2/2/1 plan and one actual briefing for this occurrence."""
    activation = turn.assistant_context_json or {}
    if activation.get("completion_requirement") != "morning_plan":
        return None
    occurrence = activation.get("occurrence")
    if not occurrence:
        return "Morning plan is incomplete: a recurring occurrence is required."
    state = db.get(AssistantState, 1)
    timezone = state.timezone if state else occurrence["timezone"]
    from app.services.assistant_planning_review import planning_review_error

    if error := planning_review_error(db, turn, now):
        return error
    status = daily_plan_status(db, timezone, now)
    if not status["planned_satisfied"]:
        counts = ", ".join(f"{key} {status['planned'][key]}/{count}" for key, count in PLAN_REQUIREMENTS.items())
        return f"Morning plan is incomplete: save today's distinct scheduled responsibilities ({counts})."
    evidence = _DayEvidence(db, timezone, now)
    candidate, seen = turn, set()
    for _ in range(64):
        if candidate is None or candidate.id in seen or candidate.session_id != turn.session_id:
            break
        seen.add(candidate.id)
        context = candidate.assistant_context_json or {}
        previous = context.get("occurrence") or {}
        if previous.get("id") != occurrence.get("id") or context.get("completion_requirement") != "morning_plan":
            break
        if evidence.output(candidate, current=candidate.id == turn.id, include_questions=True):
            return None
        candidate = db.get(ActTurn, previous.get("previous_turn_id")) if previous.get("previous_turn_id") else None
    return "Morning plan is incomplete: send the morning feed after saving the plan."
