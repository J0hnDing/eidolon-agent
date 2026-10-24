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
    PREVIOUS_MORNING_NOTES,
    PREVIOUS_STATES_MORNING_NOTES,
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


def test_planning_is_an_ordinary_task_and_only_daily_contact_has_an_output_check(db):
    planning = routine(db, "Morning planning")
    contact = routine(db, "interaction_today")
    assert planning["notes"] == MORNING_NOTES
    assert contact["recurrence"] == {"frequency": "daily", "time": "21:00", "timezone": "America/Toronto"}
    turn = start(db, "Morning planning")
    assert planning["completion_requirement"] is None
    assert "completion_requirement" not in turn.assistant_context_json
    assert turn.user_message == planning["notes"]
    assert AssistantActivationService(db).tick()["status"] == "busy"


@pytest.mark.parametrize("old_notes", [LEGACY_MORNING_NOTES, PREVIOUS_MORNING_NOTES, PREVIOUS_STATES_MORNING_NOTES], ids=["legacy", "previous", "previous-states"])
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
    assert row.title == "My morning review" and row.status == "waiting"
    assert row.revision == revision + 1
    assert row.schedule_version == schedule_version and row.due_at == due
    assert row.recurrence_json == recurrence
    assert "atlas.states.get without an at timestamp" in row.notes
    assert "Goals and Interests jointly inform the existing goal_progression State" in row.notes
    assert "personal Relationships inform the existing close_connection State" in row.notes
    AssistantService(db).state()
    db.refresh(row)
    assert row.revision == revision + 1
    AssistantService(db).update_item(row.id, AgendaUpdate(expected_revision=row.revision, status="open"))
    turn = start(db, "My morning review")
    assert turn.user_message == MORNING_NOTES


def test_states_planning_upgrade_preserves_custom_notes_one_time_work_and_deleted_routines(db):
    service = AssistantService(db)
    morning = routine(db, "Morning planning")
    custom = PREVIOUS_MORNING_NOTES + "\nMy additional instruction."
    service.update_item(morning["id"], AgendaUpdate(expected_revision=morning["revision"], notes=custom))
    one_time = service.create_item(AgendaCreate(title="One-time planning", notes=PREVIOUS_MORNING_NOTES))
    contact = routine(db, "interaction_today")
    service.delete_item(contact["id"], contact["revision"])
    before = len(service.agenda())
    service.state()
    assert db.get(AssistantAgendaItem, morning["id"]).notes == custom
    assert db.get(AssistantAgendaItem, one_time["id"]).notes == PREVIOUS_MORNING_NOTES
    assert len(service.agenda()) == before
    assert not [item for item in service.agenda() if item["completion_requirement"] == "interaction_today"]
    revised = db.get(AssistantAgendaItem, morning["id"])
    service.delete_item(revised.id, revised.revision)
    service.state()
    assert db.get(AssistantAgendaItem, morning["id"]) is None


def test_repeated_planning_can_keep_an_existing_plan_without_manufacturing_items(db):
    turn = start(db, "Morning planning")
    finish(db, turn)
    assert db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"]).status == "succeeded"


def test_saved_future_work_runs_through_the_agenda_after_planning(db):
    turn = start(db, "Morning planning")
    due = NOW + timedelta(hours=6)
    question = AssistantService(db).create_item(AgendaCreate(kind="question", question_category="temporal",
        title="Which option would you prefer?", notes="Clarify the user's preference before preparing the next step.",
        due_at=due), session_id=turn.session_id, turn_id=turn.id)
    finish(db, turn)
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
    finish(db, turn)
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


def test_recurring_outcome_cannot_be_cleared_but_removing_recurrence_clears_it(db):
    item = routine(db, "interaction_today")
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
