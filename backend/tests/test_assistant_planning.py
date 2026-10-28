from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActLiveMessage, ActSession, ActTurn, AssistantAgendaItem, AssistantAgendaOccurrence
from app.schemas.assistant import AgendaCreate, AgendaUpdate
from app.services.act_session_service import ActSessionService
from app.services.agent_memory_service import AgentMemoryService
from app.services.assistant_activation_service import AssistantActivationService
from app.services.assistant_planning import (
    LEGACY_MORNING_NOTES,
    MORNING_NOTES,
    PREVIOUS_BALANCED_MORNING_NOTES,
    PREVIOUS_BRIEFING_MORNING_NOTES,
    PREVIOUS_FLEXIBLE_MORNING_NOTES,
    PREVIOUS_MORNING_NOTES,
    PREVIOUS_REVIEW_MORNING_NOTES,
    PREVIOUS_STATES_MORNING_NOTES,
    PREVIOUS_TARGETS_MORNING_NOTES,
    daily_interaction_status,
)
from app.services.assistant_service import AssistantService
from app.services.assistant_turn_dispatcher import AssistantTurnDispatcher

NOW = datetime(2026, 10, 22, 19, tzinfo=UTC)


@pytest.fixture
def db(tmp_path, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    for module in ("assistant_planning", "assistant_activation_service", "assistant_recurrence",
                   "assistant_service", "assistant_turn_dispatcher"):
        monkeypatch.setattr(f"app.services.{module}.datetime", FrozenDatetime)
    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    monkeypatch.setattr("app.services.assistant_initialization.AssistantInitializationService.ready", lambda *_: False)
    engine = create_engine(f"sqlite:///{tmp_path / 'planning.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        AssistantService(session).state()
        yield session
    engine.dispose()


def routine(db, requirement):
    return next(item for item in AssistantService(db).agenda()
                if item["title"] == requirement or item["completion_requirement"] == requirement)


def start(db, requirement):
    item = routine(db, requirement)
    result = AssistantActivationService(db).run_now(item["id"], item["revision"])
    turn = db.get(ActTurn, result["turn_id"])
    turn.status = "running"
    db.commit()
    if turn.assistant_context_json.get("completion_requirement") == "morning_plan":
        from app.services.assistant_planning_review import record_planning_read

        record_planning_read(db, turn.id, session_id=turn.session_id, operation="notion.todo.list",
            arguments={"page_size": 100}, output={"todos": [], "has_more": False, "next_cursor": None}, now=NOW)
        record_planning_read(db, turn.id, session_id=turn.session_id, operation="atlas.goal.list",
            arguments={"limit": 100}, output={"goals": [], "graph": {}}, now=NOW)
    return turn


def finish(db, turn, message=""):
    turn.assistant_message, turn.status, turn.completed_at = message, "succeeded", NOW
    db.flush()
    AssistantTurnDispatcher()._finish_turn(db, turn)
    db.commit()
    AssistantActivationService(db).on_turn_completed(turn)


def add_output(db, *, agent="assistant", message="How has your day been?", at=NOW, backend_kind=None):
    session = ActSession(agent_id=agent, codex_thread_id=f"test-{agent}-{uuid4()}")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, status="succeeded", user_message="", assistant_message=message,
                   created_at=at - timedelta(minutes=1), completed_at=at, backend_message_kind=backend_kind)
    db.add(turn)
    db.commit()
    return turn


def save_plan(db, turn, *, interaction_ids=(), interaction_tasks=False):
    from app.services.assistant_daily_plan import save_daily_plan

    service = AssistantService(db)
    selections = {"work": [], "interaction": list(interaction_ids), "other": []}
    for category, minimum in (("work", 2), ("interaction", 2), ("other", 1)):
        for index in range(len(selections[category]), minimum):
            item = service.create_item(AgendaCreate(
                kind="question" if category == "interaction" and not interaction_tasks else "task",
                title=f"{category} responsibility {index}", notes="A distinct useful outcome and next step.",
                due_at=NOW + timedelta(minutes=30)), session_id=turn.session_id, turn_id=turn.id)
            selections[category].append(item["id"])
    save_daily_plan(db, selections, session_id=turn.session_id, turn_id=turn.id, now=NOW)
    return selections


def test_planning_is_an_ordinary_task_with_a_verifiable_plan_and_feed(db):
    planning = routine(db, "Morning planning")
    contact = routine(db, "interaction_today")
    assert planning["notes"] == MORNING_NOTES
    assert contact["recurrence"] == {"frequency": "daily", "time": "21:00", "timezone": "America/Toronto"}
    turn = start(db, "Morning planning")
    assert planning["completion_requirement"] == "morning_plan"
    assert turn.assistant_context_json["completion_requirement"] == "morning_plan"
    assert turn.user_message == planning["notes"]
    assert AssistantActivationService(db).tick()["status"] == "busy"


@pytest.mark.parametrize("old_notes", [LEGACY_MORNING_NOTES, PREVIOUS_MORNING_NOTES,
    PREVIOUS_STATES_MORNING_NOTES, PREVIOUS_TARGETS_MORNING_NOTES, PREVIOUS_BALANCED_MORNING_NOTES,
    PREVIOUS_REVIEW_MORNING_NOTES, PREVIOUS_BRIEFING_MORNING_NOTES, PREVIOUS_FLEXIBLE_MORNING_NOTES],
    ids=["legacy", "previous", "previous-states", "previous-targets", "previous-balanced", "previous-review",
         "previous-briefing", "previous-flexible"])
def test_states_planning_upgrade_reaches_migrated_installations_once(db, old_notes):
    from app.models import AssistantState

    morning = routine(db, "Morning planning")
    row = db.get(AssistantAgendaItem, morning["id"])
    row.title, row.notes, row.status = "My morning review", old_notes, "waiting"
    revision, schedule_version, due = row.revision, row.schedule_version, row.due_at
    recurrence = dict(row.recurrence_json)
    assert db.get(AssistantState, 1).planning_contract_migrated
    db.commit()
    AssistantService(db).state()
    db.refresh(row)
    assert row.notes == MORNING_NOTES
    assert row.completion_requirement == "morning_plan"
    assert row.title == "My morning review" and row.status == "waiting"
    assert row.revision == revision + 1
    assert row.schedule_version == schedule_version and row.due_at == due
    assert row.recurrence_json == recurrence
    assert "atlas.states.get without an at timestamp" in row.notes
    AssistantService(db).state()
    db.refresh(row)
    assert row.revision == revision + 1
    AssistantService(db).update_item(row.id, AgendaUpdate(expected_revision=row.revision, status="open"))
    turn = start(db, "My morning review")
    assert turn.user_message == MORNING_NOTES


@pytest.mark.parametrize("previous_notes", [PREVIOUS_MORNING_NOTES, PREVIOUS_REVIEW_MORNING_NOTES,
                                          PREVIOUS_BRIEFING_MORNING_NOTES, PREVIOUS_FLEXIBLE_MORNING_NOTES])
def test_states_planning_upgrade_preserves_custom_notes_one_time_work_and_deleted_routines(db, previous_notes):
    service = AssistantService(db)
    morning = routine(db, "Morning planning")
    custom = previous_notes + "\nMy additional instruction."
    service.update_item(morning["id"], AgendaUpdate(expected_revision=morning["revision"], notes=custom))
    one_time = service.create_item(AgendaCreate(title="One-time planning", notes=previous_notes))
    contact = routine(db, "interaction_today")
    service.delete_item(contact["id"], contact["revision"])
    before = len(service.agenda())
    service.state()
    assert db.get(AssistantAgendaItem, morning["id"]).notes == custom
    assert db.get(AssistantAgendaItem, one_time["id"]).notes == previous_notes
    assert len(service.agenda()) == before
    assert not [item for item in service.agenda() if item["completion_requirement"] == "interaction_today"]
    revised = db.get(AssistantAgendaItem, morning["id"])
    service.delete_item(revised.id, revised.revision)
    service.state()
    assert db.get(AssistantAgendaItem, morning["id"]) is None


def test_repeated_planning_can_keep_an_existing_plan_without_manufacturing_items(db):
    turn = start(db, "Morning planning")
    save_plan(db, turn)
    finish(db, turn, "Today's weather and agenda; here is what I'll handle.")
    assert db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"]).status == "succeeded"
    count = len(AssistantService(db).agenda())
    repeated = start(db, "Morning planning")
    finish(db, repeated, "Today's updated weather and agenda.")
    assert len(AssistantService(db).agenda()) == count


def test_saved_future_work_runs_through_the_agenda_after_planning(db):
    turn = start(db, "Morning planning")
    due = NOW + timedelta(hours=6)
    question = AssistantService(db).create_item(AgendaCreate(kind="question", question_category="temporal",
        title="Which option would you prefer?", notes="Clarify the user's preference before preparing the next step.",
        due_at=due), session_id=turn.session_id, turn_id=turn.id)
    save_plan(db, turn, interaction_ids=[question["id"]])
    finish(db, turn, "Today's weather and agenda; here is what I'll handle.")
    assert db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"]).status == "succeeded"
    assert db.get(AssistantAgendaItem, question["id"]).status == "open"
    assert not list(db.scalars(select(ActLiveMessage)))
    # A saved follow-up is real future work; planning must not ask it prematurely.
    for item in AssistantService(db).agenda(active_only=True):
        if item["recurrence"]:
            AssistantService(db).update_item(item["id"], AgendaUpdate(expected_revision=item["revision"], status="waiting"))
    assert AssistantActivationService(db).tick()["status"] == "idle"
    db.get(AssistantAgendaItem, question["id"]).due_at = NOW - timedelta(minutes=1)
    db.commit()
    result = AssistantActivationService(db).tick()
    activation = db.get(ActTurn, result["turn_id"]).assistant_context_json
    assert [item["id"] for item in activation["agenda"]] == [question["id"]]


def test_planning_accepts_explicit_waiting_dependency_and_keeps_history(db):
    turn = start(db, "Morning planning")
    item = AssistantService(db).create_item(AgendaCreate(title="Prepare application for selected programs",
        status="waiting", notes="Depends on the user's program selections in question 2; next prepare a document checklist."),
        turn_id=turn.id)
    save_plan(db, turn)
    finish(db, turn, "Today's weather and agenda; here is what I'll handle.")
    changed = AssistantService(db).update_item(item["id"], AgendaUpdate(expected_revision=item["revision"], status="done"))
    assert changed["status"] == "done"
    assert any(row["id"] == item["id"] for row in AssistantService(db).agenda())


def test_silent_daily_interaction_fails_and_retries_without_claiming_success(db):
    turn = start(db, "interaction_today")
    with pytest.raises(ValueError, match="Daily interaction is incomplete"):
        finish(db, turn)
    # The reconciliation path enforces the same outcome if a terminal status
    # was committed before a crash could run the normal finish hook.
    db.commit()
    AssistantActivationService(db).on_turn_completed(turn)
    occurrence = db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"])
    assert occurrence.status == "failed"
    assert turn.status == "failed"
    assert occurrence.retry_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=15)


@pytest.mark.parametrize("saved", [False, True])
def test_morning_deficit_fails_normal_finish_and_crash_reconciliation(db, saved):
    turn = start(db, "Morning planning")
    if saved:
        save_plan(db, turn)
    with pytest.raises(ValueError, match="Morning plan is incomplete"):
        finish(db, turn, "" if saved else "A briefing alone is not enough.")
    db.commit()
    AssistantActivationService(db).on_turn_completed(turn)
    occurrence = db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"])
    assert turn.status == "failed" and occurrence.status == "failed"
    assert occurrence.retry_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=15)


@pytest.mark.parametrize("existing", [False, True])
def test_daily_interaction_accepts_own_final_response_and_existing_contact(db, existing):
    if existing:
        add_output(db)
    turn = start(db, "interaction_today")
    finish(db, turn, "" if existing else "How has your day been?")
    assert daily_interaction_status(db, "America/Toronto", NOW)["satisfied"]
    assert db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"]).status == "succeeded"


def test_interaction_status_uses_local_date_and_excludes_silent_other_agent_and_refresh(db):
    add_output(db, agent="act")
    add_output(db, message="")
    add_output(db, backend_kind="assistant_refresh")
    add_output(db, at=datetime(2026, 10, 22, 3, 59, tzinfo=UTC))  # Previous Toronto day.
    assert daily_interaction_status(db, "America/Toronto", NOW)["count"] == 0
    add_output(db, at=datetime(2026, 10, 22, 4, tzinfo=UTC))
    assert daily_interaction_status(db, "America/Toronto", NOW)["count"] == 1


def test_live_question_counts_after_canonical_archive_erases_database_text(db):
    turn = start(db, "interaction_today")
    live = ActLiveMessage(turn_id=turn.id, role="assistant", text="How has your day been?", created_at=NOW)
    db.add(live)
    db.commit()
    finish(db, turn)
    AgentMemoryService(db).sync_chat_history(now=NOW)
    db.refresh(turn)
    db.refresh(live)
    assert live.text == "" and turn.history_archived_at is not None
    status = daily_interaction_status(db, "America/Toronto", NOW)
    assert status["count"] == 1 and status["satisfied"]
    assert AssistantService(db).context(turn.session_id)["daily_interaction"]["satisfied"]


def test_migration_upgrades_only_untouched_defaults_and_does_not_recreate_deleted_check_in(db):
    from app.models import AssistantState
    from app.services.assistant_planning import migrate_planning_contract

    service = AssistantService(db)
    morning = routine(db, "Morning planning")
    service.update_item(morning["id"], AgendaUpdate(expected_revision=morning["revision"], notes="My custom planning."))
    untouched = service.create_item(AgendaCreate(title="Legacy planning", notes=LEGACY_MORNING_NOTES,
        recurrence={"frequency": "daily", "time": "08:00", "timezone": "America/Toronto"}))
    state = db.get(AssistantState, 1)
    # Simulate the pre-upgrade agenda without an already seeded check-in.
    contact = routine(db, "interaction_today")
    service.delete_item(contact["id"], contact["revision"])
    state.planning_contract_migrated = False
    db.commit()
    migrate_planning_contract(db, state)
    assert db.get(AssistantAgendaItem, morning["id"]).notes == "My custom planning."
    assert db.get(AssistantAgendaItem, untouched["id"]).notes == MORNING_NOTES
    contact = routine(db, "interaction_today")
    service.delete_item(contact["id"], contact["revision"])
    service.state()
    assert not [item for item in service.agenda() if item["completion_requirement"] == "interaction_today"]


@pytest.mark.parametrize("requirement", ["interaction_today", "morning_plan"])
def test_recurring_outcome_cannot_be_cleared_but_removing_recurrence_clears_it(db, requirement):
    item = routine(db, requirement)
    with pytest.raises(ValueError, match="Remove recurrence"):
        AssistantService(db).update_item(item["id"], AgendaUpdate(
            expected_revision=item["revision"], completion_requirement=None))
    changed = AssistantService(db).update_item(item["id"], AgendaUpdate(
        expected_revision=item["revision"], recurrence=None))
    assert changed["recurrence"] is None and changed["completion_requirement"] is None


def test_daily_completion_uses_current_assistant_timezone_after_timezone_edit(db):
    from app.models import AssistantState

    db.get(AssistantState, 1).timezone = "Asia/Tokyo"
    db.commit()
    add_output(db, at=datetime(2026, 10, 22, 10, tzinfo=UTC))  # Toronto today, Tokyo yesterday.
    assert not daily_interaction_status(db, "Asia/Tokyo", NOW)["satisfied"]
    turn = start(db, "interaction_today")
    with pytest.raises(ValueError, match="Daily interaction is incomplete"):
        finish(db, turn)


def test_upgrade_reuses_existing_check_in_and_preserves_custom_planning(db):
    from app.models import AssistantState
    from app.services.assistant_planning import migrate_planning_contract

    morning = routine(db, "Morning planning")
    row = db.get(AssistantAgendaItem, morning["id"])
    row.notes, row.completion_requirement = "My own morning procedure.", None
    before = len(AssistantService(db).agenda())
    state = db.get(AssistantState, 1)
    state.planning_contract_migrated = False
    db.commit()
    migrate_planning_contract(db, state)
    assert len(AssistantService(db).agenda()) == before
    assert row.notes == "My own morning procedure."
    assert row.completion_requirement is None


def test_planned_casual_interactions_run_separately_and_unfinished_work_wakes_again(db):
    service = AssistantService(db)
    turn = start(db, "Morning planning")
    selections = save_plan(db, turn, interaction_tasks=True)
    finish(db, turn, "Today's weather and agenda; here is what I'll handle.")
    for item in service.agenda():
        if item["recurrence"]:
            service.update_item(item["id"], AgendaUpdate(expected_revision=item["revision"], status="waiting"))
        else:
            service.update_item(item["id"], AgendaUpdate(expected_revision=item["revision"], due_at=NOW))
    activation = AssistantActivationService(db)
    for interaction_id in selections["interaction"]:
        result = activation.tick()
        conversation = db.get(ActTurn, result["turn_id"])
        assert [snapshot["id"] for snapshot in conversation.assistant_context_json["agenda"]] == [interaction_id]
        conversation.status = "running"
        item = db.get(AssistantAgendaItem, interaction_id)
        service.update_item(item.id, AgendaUpdate(expected_revision=item.revision, status="done"), turn_id=conversation.id)
        finish(db, conversation, "A personal conversation started here.")
    plan = activation.status()["daily_plan"]
    assert plan["completed"]["interaction"] == 2
    assert plan["completed"]["work"] == 0
    result = activation.tick()
    execution = db.get(ActTurn, result["turn_id"])
    execution.status = "running"
    finish(db, execution)
    for item_id in selections["work"] + selections["other"]:
        item = db.get(AssistantAgendaItem, item_id)
        assert item.status == "open"
        assert item.due_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=15)


def test_silent_casual_interaction_cannot_be_completed_by_changing_status(db):
    service = AssistantService(db)
    turn = start(db, "Morning planning")
    selections = save_plan(db, turn, interaction_tasks=True)
    finish(db, turn, "Today's weather and agenda.")
    for item in service.agenda():
        if item["recurrence"]:
            service.update_item(item["id"], AgendaUpdate(expected_revision=item["revision"], status="waiting"))
    item = db.get(AssistantAgendaItem, selections["interaction"][0])
    service.update_item(item.id, AgendaUpdate(expected_revision=item.revision, due_at=NOW))
    result = AssistantActivationService(db).tick()
    interaction = db.get(ActTurn, result["turn_id"])
    interaction.status = "running"
    service.update_item(item.id, AgendaUpdate(expected_revision=item.revision, status="done"), turn_id=interaction.id)
    finish(db, interaction)
    assert item.status == "open"
    assert item.due_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=15)
    assert AssistantActivationService(db).status()["daily_plan"]["completed"]["interaction"] == 0


def test_morning_cannot_skip_fresh_sources_despite_a_saved_plan_and_feed(db, monkeypatch):
    monkeypatch.setattr("app.services.function_catalog_service.FunctionCatalogService.list_entries", lambda *_: [
        {"id": operation, "category": "integration", "availability": "available", "mcp_exposed": True,
         "risk_level": "low", "mcp_read_only": True}
        for operation in ("notion.todo.list", "atlas.goal.list", "atlas.records.search")])
    turn = start(db, "Morning planning")
    save_plan(db, turn)
    turn.assistant_review_json = {}
    db.commit()
    with pytest.raises(ValueError, match="Morning plan is incomplete"):
        finish(db, turn, "A feed and five items alone are not enough.")


def test_morning_can_ask_a_tracked_question_and_finish_without_a_second_message(db, monkeypatch):
    from app.services import assistant_planning
    from app.services.assistant_questions import AssistantQuestionService

    monkeypatch.setattr("app.services.assistant_questions.datetime", assistant_planning.datetime)
    monkeypatch.setattr("app.models.entities.datetime", assistant_planning.datetime)
    turn = start(db, "Morning planning")
    save_plan(db, turn)
    service = AssistantService(db)
    question = service.create_item(AgendaCreate(kind="question", title="Which applications still need help?",
        due_at=NOW), session_id=turn.session_id, turn_id=turn.id)
    questions = AssistantQuestionService(db)
    sent = questions.ask(question["id"], question["title"], question["revision"],
        session_id=turn.session_id, turn_id=turn.id)
    repeated = questions.ask(question["id"], question["title"], question["revision"],
        session_id=turn.session_id, turn_id=turn.id)
    assert sent["status"] == "sent" and repeated["status"] == "already_asked"
    assert repeated["message_id"] == sent["message_id"]
    finish(db, turn)
    assert turn.status == "succeeded"
    occurrence = db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"])
    assert occurrence.status == "succeeded" and occurrence.retry_at is None
    assert db.get(AssistantAgendaItem, question["id"]).status == "waiting"
    AgentMemoryService(db).sync_chat_history(now=NOW)
    db.refresh(turn)
    message = db.get(ActLiveMessage, sent["message_id"])
    db.refresh(message)
    assert message.text == "" and turn.history_archived_at is not None
    assert AgentMemoryService(db).read_turn_content(turn, agent_id="assistant")["live_messages"][message.id] == question["title"]
    assert len(list(db.scalars(select(ActLiveMessage).where(ActLiveMessage.turn_id == turn.id)))) == 1
    assert AssistantActivationService(db).status()["daily_plan"]["completed"]["interaction"] == 0


def test_morning_prompt_requires_backlog_and_goals_regardless_of_changes():
    assert "Read current todos and goals every morning, even when nothing changed" in MORNING_NOTES
    assert "notion.todo.list(page_size=100)" in MORNING_NOTES
    assert "atlas.goal.list(limit=100) without pursuit_status or horizon filters" in MORNING_NOTES
    assert "Keep these notes as the reusable procedure" in MORNING_NOTES


def test_finish_reads_mcp_process_receipts_without_losing_pending_final_output(db):
    turn = start(db, "Morning planning")
    turn.assistant_review_json = {}
    db.commit()
    assert turn.assistant_review_json == {}
    # Keep the dispatcher's ORM row loaded while MCP uses its own session.
    assert "daily_plan" not in turn.assistant_context_json
    with Session(db.get_bind()) as mcp:
        from app.services.assistant_planning_review import record_planning_read

        tool_turn = mcp.get(ActTurn, turn.id)
        record_planning_read(mcp, turn.id, session_id=turn.session_id, operation="notion.todo.list",
            arguments={"page_size": 100}, output={"todos": [], "has_more": False, "next_cursor": None}, now=NOW)
        record_planning_read(mcp, turn.id, session_id=turn.session_id, operation="atlas.goal.list",
            arguments={"limit": 100}, output={"goals": [], "graph": {}}, now=NOW)
        save_plan(mcp, tool_turn)
    assert "daily_plan" not in turn.assistant_context_json
    assert turn.assistant_review_json == {}
    feed = "Today's weather and agenda; here is what I'll handle."
    finish(db, turn, feed)
    assert turn.assistant_message == feed
    occurrence = db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"])
    assert occurrence.status == "succeeded"
