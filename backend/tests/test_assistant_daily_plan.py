from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActLiveMessage, ActSession, ActTurn, AssistantAgendaItem, AssistantState
from app.services.agent_memory_service import AgentMemoryService
from app.services.assistant_daily_plan import daily_plan_status, morning_plan_error, save_daily_plan

NOW = datetime(2026, 10, 25, 14, tzinfo=UTC)
TIMEZONE = "America/Toronto"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'daily-plan.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(AssistantState(id=1, enabled=True, timezone=TIMEZONE,
                                   agenda_schedules_migrated=True, planning_contract_migrated=True))
        session.add(ActSession(id=1, agent_id="assistant", status="active", codex_thread_id="test"))
        session.flush()
        session.add(ActTurn(id=1, session_id=1, status="running", user_message="Plan today", created_at=NOW,
                            assistant_context_json={"completion_requirement": "morning_plan", "occurrence": {
                                "id": 10, "agenda_item_id": 10, "timezone": TIMEZONE}}))
        for item_id in range(1, 6):
            session.add(AssistantAgendaItem(id=item_id, title=f"Responsibility {item_id}",
                                            kind="question" if item_id in (3, 4) else "task",
                                            due_at=NOW + timedelta(hours=item_id)))
        session.commit()
        from app.services.assistant_planning_review import record_planning_read

        record_planning_read(session, 1, session_id=1, operation="notion.todo.list",
            arguments={"page_size": 100}, output={"todos": [], "has_more": False, "next_cursor": None}, now=NOW)
        record_planning_read(session, 1, session_id=1, operation="atlas.goal.list",
            arguments={"limit": 100}, output={"goals": [], "graph": {}}, now=NOW)
        yield session
    engine.dispose()


def selection():
    return {"work": [1, 2], "interaction": [3, 4], "other": [5]}


def save(db, payload=None, *, now=NOW, turn_id=1):
    return save_daily_plan(db, payload or selection(), session_id=1, turn_id=turn_id, now=now)


def activation(db, item_ids, *, status="succeeded", message="", at=NOW, agent="assistant"):
    session_id = 1
    if agent != "assistant":
        session = ActSession(agent_id=agent, status="active", codex_thread_id=f"test-{agent}")
        db.add(session)
        db.flush()
        session_id = session.id
    turn = ActTurn(session_id=session_id, status=status, user_message="Execute agenda", assistant_message=message,
                   created_at=at, completed_at=at if status == "succeeded" else None,
                   assistant_context_json={"kind": "agenda", "agenda": [{"id": item_id} for item_id in item_ids]})
    db.add(turn)
    db.commit()
    return turn


def test_save_records_existing_distinct_items_and_keeps_categories_out_of_agenda(db):
    status = save(db)
    assert status["planned_satisfied"] and status["planned"] == {"work": 2, "interaction": 2, "other": 1}
    assert not status["follow_through_satisfied"]
    assert status["completed"] == {"work": 0, "interaction": 0, "other": 0}
    receipt = db.get(ActTurn, 1).assistant_context_json["daily_plan"]
    assert receipt["day"] == "2026-10-25" and receipt["timezone"] == TIMEZONE
    assert receipt["items"]["work"][0]["scheduled_at"] == "2026-10-25T15:00:00+00:00"
    assert db.get(AssistantAgendaItem, 1).revision == 1
    assert save(db)["items"] == status["items"]


@pytest.mark.parametrize("payload", [
    {"work": [1], "interaction": [3, 4], "other": [5]},
    {"work": [1, 2], "interaction": [3], "other": [5]},
    {"work": [1, 2], "interaction": [3, 4], "other": []},
    {"work": [1, 2], "interaction": [3, 4], "other": [1]},
    {"work": [1, 1], "interaction": [3, 4], "other": [5]},
    {"work": [True, 2], "interaction": [3, 4], "other": [5]},
    {"work": [1, 2], "interaction": [3, 4], "other": [0]},
    {"work": [1, 2], "interaction": [3, 4], "other": [5], "extra": []},
    {"work": list(range(1, 30)), "interaction": [30, 31], "other": [32]},
])
def test_invalid_selection_does_not_persist_a_receipt(db, payload):
    with pytest.raises(ValueError):
        save(db, payload)
    assert "daily_plan" not in db.get(ActTurn, 1).assistant_context_json


@pytest.mark.parametrize("change", ["future", "yesterday", "unscheduled", "recurring", "dismissed", "deleted", "question"])
def test_unsuitable_work_is_rejected(db, change):
    item = db.get(AssistantAgendaItem, 1)
    if change == "future":
        item.due_at = NOW + timedelta(days=1)
    elif change == "yesterday":
        item.due_at = NOW - timedelta(days=1)
    elif change == "unscheduled":
        item.due_at = None
    elif change == "recurring":
        item.recurrence_json = {"frequency": "daily", "time": "11:00", "timezone": TIMEZONE}
    elif change == "dismissed":
        item.status = "dismissed"
    elif change == "deleted":
        db.delete(item)
    else:
        item.kind = "question"
    db.commit()
    with pytest.raises(ValueError, match="nonrecurring work"):
        save(db)


@pytest.mark.parametrize("change", ["agent", "session", "status", "cancelled", "disabled"])
def test_only_current_enabled_running_assistant_can_save(db, change):
    if change == "agent":
        db.get(ActSession, 1).agent_id = "act"
    elif change == "session":
        db.get(ActSession, 1).status = "archived"
    elif change == "status":
        db.get(ActTurn, 1).status = "succeeded"
    elif change == "cancelled":
        db.get(ActTurn, 1).cancel_requested_at = NOW
    else:
        db.get(AssistantState, 1).enabled = False
    db.commit()
    with pytest.raises(ValueError, match="current active running Assistant"):
        save(db)


def test_activation_clearing_due_time_does_not_erase_the_plan(db):
    save(db)
    item = db.get(AssistantAgendaItem, 1)
    item.due_at = None
    db.commit()
    execution = activation(db, [1], status="running")
    status = daily_plan_status(db, TIMEZONE, NOW)
    assert status["planned_satisfied"] and status["completed"]["work"] == 0
    execution.status, execution.completed_at = "succeeded", NOW
    item.status = "done"
    db.commit()
    status = daily_plan_status(db, TIMEZONE, NOW)
    assert status["completed"]["work"] == 1
    assert save(db)["planned_satisfied"]  # Reviewing existing work needs no duplicate task.


def test_claimed_completion_without_an_actual_activation_does_not_count(db):
    save(db)
    item = db.get(AssistantAgendaItem, 1)
    item.status, item.due_at, item.last_turn_id = "done", None, 1
    db.commit()
    assert daily_plan_status(db, TIMEZONE, NOW)["completed"]["work"] == 0
    with pytest.raises(ValueError, match="nonrecurring work"):
        save(db)
    activation(db, [1], agent="act")
    with pytest.raises(ValueError, match="nonrecurring work"):
        save(db)


def test_future_rescheduling_deletion_and_dismissal_create_inspectable_deficits(db):
    save(db)
    db.get(AssistantAgendaItem, 1).due_at = NOW + timedelta(days=1)
    db.get(AssistantAgendaItem, 2).status = "dismissed"
    db.delete(db.get(AssistantAgendaItem, 5))
    db.commit()
    status = daily_plan_status(db, TIMEZONE, NOW)
    assert status["planned"] == {"work": 0, "interaction": 2, "other": 0}
    assert status["items"]["other"][0]["status"] == "deleted"
    assert not status["planned_satisfied"]


def test_waiting_question_needs_todays_receipt_and_blocked_work_cannot_fill_the_plan(db):
    question, work = db.get(AssistantAgendaItem, 3), db.get(AssistantAgendaItem, 1)
    question.status = "waiting"
    db.commit()
    with pytest.raises(ValueError, match="nonrecurring interaction"):
        save(db)
    db.add(ActLiveMessage(turn_id=1, role="assistant", agenda_question_id=3, text="How are you?", created_at=NOW))
    work.status, work.due_at = "waiting", None
    db.commit()
    with pytest.raises(ValueError, match="nonrecurring work"):
        save(db)
    work.due_at = NOW + timedelta(hours=1)
    db.commit()
    with pytest.raises(ValueError, match="nonrecurring work"):
        save(db)
    activation(db, [1])
    assert save(db)["completed"]["interaction"] == 1


def test_question_receipts_survive_waiting_answers_and_archived_message_text(db):
    save(db)
    turn = activation(db, [3, 4])
    for item_id in (3, 4):
        item = db.get(AssistantAgendaItem, item_id)
        item.status, item.due_at = "waiting", None
        db.add(ActLiveMessage(turn_id=turn.id, role="assistant", agenda_question_id=item_id,
                              text=f"Question {item_id}", created_at=NOW))
    db.commit()
    AgentMemoryService(db).sync_chat_history(now=NOW)
    assert daily_plan_status(db, TIMEZONE, NOW)["completed"]["interaction"] == 2


def test_two_interaction_tasks_need_distinct_delivered_activations(db):
    for item_id in (3, 4):
        db.get(AssistantAgendaItem, item_id).kind = "task"
    db.commit()
    save(db)
    first = activation(db, [3, 4], message="Something you might enjoy")
    for item_id in (3, 4):
        item = db.get(AssistantAgendaItem, item_id)
        item.status, item.due_at = "done", None
    db.commit()
    assert daily_plan_status(db, TIMEZONE, NOW)["completed"]["interaction"] == 1
    activation(db, [4], message="How did the afternoon go?")
    AgentMemoryService(db).sync_chat_history(now=NOW)
    db.refresh(first)
    assert first.assistant_message is None or first.assistant_message == ""
    assert daily_plan_status(db, TIMEZONE, NOW)["completed"]["interaction"] == 2


def test_feed_and_unrelated_chat_do_not_count_as_personal_interactions(db):
    save(db)
    morning = db.get(ActTurn, 1)
    morning.assistant_message = "Today's weather and agenda"
    morning.status, morning.completed_at = "succeeded", NOW
    db.commit()
    activation(db, [], message="Ordinary unrelated reply")
    assert daily_plan_status(db, TIMEZONE, NOW)["completed"]["interaction"] == 0


def test_complete_plan_requires_actual_work_and_both_interaction_deliveries(db):
    save(db)
    turn = activation(db, [1, 2, 3, 4, 5])
    for item_id in (1, 2, 5):
        item = db.get(AssistantAgendaItem, item_id)
        item.status, item.due_at = "done", None
    for item_id in (3, 4):
        item = db.get(AssistantAgendaItem, item_id)
        item.status, item.due_at = "waiting", None
        db.add(ActLiveMessage(turn_id=turn.id, role="assistant", agenda_question_id=item_id,
                              text="Personal question", created_at=NOW))
    db.commit()
    assert daily_plan_status(db, TIMEZONE, NOW)["follow_through_satisfied"]


@pytest.mark.parametrize("now,timezone", [
    (datetime(2026, 10, 26, 3, 59, tzinfo=UTC), TIMEZONE),  # Still Toronto's 25th.
    (datetime(2026, 10, 25, 14, tzinfo=UTC), "Asia/Tokyo"),
    (datetime(2026, 10, 26, 4, tzinfo=UTC), TIMEZONE),
])
def test_receipt_is_scoped_to_assistant_local_date_and_timezone(db, now, timezone):
    save(db)
    status = daily_plan_status(db, timezone, now)
    assert status["planned_satisfied"] is (timezone == TIMEZONE and now.day == 26 and now.hour == 3)


def test_plan_date_survives_both_dst_folds_and_expires_at_the_correct_midnight(db):
    first_fold = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    for item_id in range(1, 6):
        db.get(AssistantAgendaItem, item_id).due_at = first_fold + timedelta(hours=item_id)
    db.commit()
    save(db, now=first_fold)
    assert daily_plan_status(db, TIMEZONE, first_fold + timedelta(hours=1))["planned_satisfied"]
    assert daily_plan_status(db, TIMEZONE, datetime(2026, 11, 2, 4, 59, tzinfo=UTC))["planned_satisfied"]
    assert not daily_plan_status(db, TIMEZONE, datetime(2026, 11, 2, 5, tzinfo=UTC))["planned_satisfied"]


def test_morning_requires_plan_and_its_own_briefing(db):
    turn = db.get(ActTurn, 1)
    assert "save today's" in morning_plan_error(db, turn, NOW)
    save(db)
    assert "send the morning feed" in morning_plan_error(db, turn, NOW)
    activation(db, [], message="An unrelated answer")
    assert "send the morning feed" in morning_plan_error(db, turn, NOW)
    turn.assistant_message = "Weather and today's agenda"
    assert morning_plan_error(db, turn, NOW) is None


def test_tracked_question_can_be_the_morning_message_without_completing_other_interactions(db):
    save(db)
    db.add(AssistantAgendaItem(id=6, title="Which applications still need help?", kind="question", status="waiting"))
    db.add(ActLiveMessage(turn_id=1, role="assistant", agenda_question_id=6,
                          text="Which applications still need help?", created_at=NOW))
    db.commit()
    assert morning_plan_error(db, db.get(ActTurn, 1), NOW) is None
    assert daily_plan_status(db, TIMEZONE, NOW)["completed"]["interaction"] == 0


@pytest.mark.parametrize("text,created_at", [("", NOW), ("   ", NOW), ("An older question", NOW - timedelta(days=1))])
def test_empty_or_old_tracked_question_is_not_a_morning_message(db, text, created_at):
    save(db)
    db.add(ActLiveMessage(turn_id=1, role="assistant", agenda_question_id=3, text=text, created_at=created_at))
    db.commit()
    assert "send the morning feed" in morning_plan_error(db, db.get(ActTurn, 1), NOW)


@pytest.mark.parametrize("tracked_question", [False, True])
def test_retry_reuses_prior_selection_and_live_feed_without_resending(db, tracked_question):
    save(db)
    previous = db.get(ActTurn, 1)
    previous.status, previous.completed_at = "failed", NOW
    message = ActLiveMessage(turn_id=1, role="assistant", text="Today's morning message", created_at=NOW,
                             agenda_question_id=3 if tracked_question else None)
    db.add(message)
    db.commit()
    retry = ActTurn(session_id=1, status="running", user_message="Retry planning", created_at=NOW,
                    assistant_context_json={"completion_requirement": "morning_plan", "occurrence": {
                        "id": 10, "agenda_item_id": 10, "timezone": TIMEZONE, "previous_turn_id": 1}})
    db.add(retry)
    db.commit()
    from app.services.assistant_planning_review import record_planning_read

    for operation, arguments, output in (
        ("notion.todo.list", {"page_size": 100}, {"todos": [], "has_more": False, "next_cursor": None}),
        ("atlas.goal.list", {"limit": 100}, {"goals": [], "graph": {}}),
    ):
        record_planning_read(db, retry.id, session_id=1, operation=operation, arguments=arguments, output=output, now=NOW)
    assert morning_plan_error(db, retry, NOW) is None
    assert daily_plan_status(db, TIMEZONE, NOW)["plan_turn_id"] == 1
    AgentMemoryService(db).sync_chat_history(now=NOW)
    assert morning_plan_error(db, retry, NOW) is None
    assert len(list(db.scalars(select(ActLiveMessage)))) == 1


def test_retry_does_not_reuse_other_occurrence_or_failed_undelivered_final(db):
    save(db)
    previous = db.get(ActTurn, 1)
    previous.status, previous.completed_at, previous.assistant_message = "failed", NOW, "An unsent final"
    retry = ActTurn(session_id=1, status="running", user_message="Retry planning", created_at=NOW,
                    assistant_context_json={"completion_requirement": "morning_plan", "occurrence": {
                        "id": 10, "timezone": TIMEZONE, "previous_turn_id": 1}})
    db.add(retry)
    db.commit()
    from app.services.assistant_planning_review import record_planning_read

    record_planning_read(db, retry.id, session_id=1, operation="notion.todo.list",
        arguments={"page_size": 100}, output={"todos": [], "has_more": False, "next_cursor": None}, now=NOW)
    record_planning_read(db, retry.id, session_id=1, operation="atlas.goal.list",
        arguments={"limit": 100}, output={"goals": [], "graph": {}}, now=NOW)
    assert "send the morning feed" in morning_plan_error(db, retry, NOW)
    previous.status = "succeeded"
    previous.assistant_context_json = {**previous.assistant_context_json,
                                       "occurrence": {"id": 11, "timezone": TIMEZONE}}
    db.commit()
    assert "send the morning feed" in morning_plan_error(db, retry, NOW)
