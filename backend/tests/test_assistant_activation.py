import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    ActLiveMessage,
    ActSession,
    ActTurn,
    AgentProposal,
    AssistantAgendaItem,
    TelegramBotConnection,
    TelegramTopicSession,
)
from app.schemas.assistant import AgendaCreate
from app.services.act_session_service import ActSessionService
from app.services.agent_memory_service import AgentMemoryService
from app.services.agent_turn_delivery import deliver_agent_turn_result
from app.services.assistant_activation_service import AssistantActivationService
from app.services.assistant_service import AssistantService
from app.services.assistant_turn_dispatcher import AssistantTurnDispatcher


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'activation.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    monkeypatch.setattr("app.services.assistant_sources.collect_sources", lambda *_a, **_k: {})
    monkeypatch.setattr("app.services.assistant_initialization.AssistantInitializationService.ready", lambda *_: False)
    with Session(engine) as session:
        service = AssistantService(session)
        service.state()
        for item in service.agenda():
            service.delete_item(item["id"], item["revision"])
        yield session
    engine.dispose()


def freeze(monkeypatch, value):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr("app.services.assistant_activation_service.datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.assistant_service.datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.assistant_recurrence.datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.assistant_turn_dispatcher.datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.assistant_planning.datetime", FrozenDatetime)


def activate(db):
    db.add(AssistantAgendaItem(title="Review current responsibilities", due_at=datetime(2020, 1, 1, tzinfo=UTC)))
    db.commit()
    return AssistantActivationService(db).tick()


def finish(db, turn_id, status="succeeded"):
    turn = db.get(ActTurn, turn_id)
    turn.status = status
    turn.completed_at = datetime.now(UTC)
    db.commit()
    return turn


def test_due_agenda_and_outcomes_coalesce_and_reuse_primary(db, monkeypatch):
    now = datetime(2026, 10, 16, 14, tzinfo=UTC)
    freeze(monkeypatch, now)
    source = ActSessionService(db, agent_id="assistant").create_session()
    item = AssistantAgendaItem(title="Prepare application", due_at=now - timedelta(hours=1))
    proposal = AgentProposal(source_session_id=source.id, title="Draft", rationale="Prepare",
                             instruction="Draft", actions="Draft", fingerprint="draft",
                             execution_status="succeeded", execution_state_json={"completed_steps": 1})
    db.add_all([item, proposal])
    db.commit()

    service = AssistantActivationService(db)
    result = service.tick()
    turn = db.get(ActTurn, result["turn_id"])
    assert result["kind"] == "agenda"
    assert result["session_id"] == source.id
    assert turn.assistant_context_json["agenda"][0]["id"] == item.id
    assert turn.assistant_context_json["executions"][0]["proposal_id"] == proposal.id
    assert item.due_at is None
    assert proposal.execution_state_json["assistant_outcome_turn_id"] == turn.id
    assert service.tick()["status"] == "busy"
    finish(db, turn.id)
    assert service.tick()["status"] == "idle"
    assert len(list(db.scalars(select(ActSession)))) == 1


def test_busy_conversation_preserves_due_items_and_accepts_user_messages(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    service = AssistantActivationService(db)
    result = activate(db)
    sessions = ActSessionService(db, agent_id="assistant")
    reply = sessions.enqueue_turn(result["session_id"], "I have a question")
    assert reply.id != result["turn_id"]
    item = AssistantAgendaItem(title="Check result", due_at=datetime(2026, 10, 15, tzinfo=UTC))
    db.add(item)
    db.commit()
    assert service.tick()["status"] == "busy"
    assert item.due_at is not None
    finish(db, result["turn_id"])
    finish(db, reply.id)
    assert service.tick()["kind"] == "agenda"


def test_concurrent_ticks_claim_only_one_activation(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    db.add(AssistantAgendaItem(title="Due work", due_at=datetime(2026, 10, 16, tzinfo=UTC)))
    db.commit()
    factory = sessionmaker(bind=db.get_bind())

    def activate():
        with factory() as other:
            return AssistantActivationService(other).tick()["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: activate(), range(2)))
    assert sorted(statuses) == ["busy", "queued"]
    assert len(list(db.scalars(select(ActTurn)))) == 1


def test_assistant_reply_can_resume_same_turn_wait(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    result = activate(db)
    turn = db.get(ActTurn, result["turn_id"])
    turn.status = "running"
    prompt = ActLiveMessage(turn_id=turn.id, role="assistant", text="Which account?",
                            reply_status="awaiting", reply_expires_at=datetime.now(UTC) + timedelta(minutes=5))
    db.add(prompt)
    db.commit()
    reply = ActSessionService(db, agent_id="assistant").enqueue_turn(turn.session_id, "Personal account")
    assert reply.id == turn.id
    assert db.get(ActLiveMessage, prompt.id).reply_status == "answered"
    assert len(list(db.scalars(select(ActTurn)))) == 1


def test_downtime_coalesces_previous_occurrence_without_suppressing_today(db, monkeypatch):
    service = AssistantActivationService(db)
    freeze(monkeypatch, datetime(2026, 10, 13, 12, tzinfo=UTC))
    item = AssistantService(db).create_item(AgendaCreate(title="Morning planning", recurrence={
        "frequency": "daily", "time": "09:00", "timezone": "America/Toronto"}))
    freeze(monkeypatch, datetime(2026, 10, 16, 12, tzinfo=UTC))  # 08:00 Toronto
    result = service.tick()
    assert result["kind"] == "agenda"
    turn = finish(db, result["turn_id"])
    assert turn.assistant_context_json["occurrence"]["agenda_item_id"] == item["id"]
    assert datetime.fromisoformat(turn.assistant_context_json["occurrence"]["scheduled_at"]) == datetime(2026, 10, 15, 13, tzinfo=UTC)
    service.on_turn_completed(turn)
    assert service.tick()["status"] == "idle"
    freeze(monkeypatch, datetime(2026, 10, 16, 13, tzinfo=UTC))
    assert service.tick()["kind"] == "agenda"


def test_ordinary_reflection_dst_repeated_hour_runs_once(db, monkeypatch):
    service = AssistantActivationService(db)
    freeze(monkeypatch, datetime(2026, 11, 1, 3, tzinfo=UTC))
    AssistantService(db).create_item(AgendaCreate(title="Evening reflection", recurrence={
        "frequency": "daily", "time": "01:30", "timezone": "America/Toronto"}))
    freeze(monkeypatch, datetime(2026, 11, 1, 5, 40, tzinfo=UTC))
    result = service.tick()
    assert result["kind"] == "agenda"
    turn = finish(db, result["turn_id"])
    service.on_turn_completed(turn)
    freeze(monkeypatch, datetime(2026, 11, 1, 6, 40, tzinfo=UTC))
    assert service.tick()["status"] == "idle"
    assert "next_reflection_at" not in service.status()


def test_nonexistent_dst_time_waits_until_its_resolved_instant(db, monkeypatch):
    service = AssistantActivationService(db)
    freeze(monkeypatch, datetime(2026, 3, 8, 5, tzinfo=UTC))
    AssistantService(db).create_item(AgendaCreate(title="Review", recurrence={
        "frequency": "daily", "time": "02:30", "timezone": "America/Toronto"}))
    freeze(monkeypatch, datetime(2026, 3, 8, 7, 0, tzinfo=UTC))
    assert service.tick()["status"] == "idle"
    freeze(monkeypatch, datetime(2026, 3, 8, 7, 30, tzinfo=UTC))
    assert service.tick()["kind"] == "agenda"


def test_failed_activation_leaves_agenda_waiting_without_replaying_actions(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    item = AssistantAgendaItem(title="Check submission", due_at=datetime(2026, 10, 16, tzinfo=UTC))
    db.add(item)
    db.commit()
    service = AssistantActivationService(db)
    result = service.tick()
    turn = finish(db, result["turn_id"], "interrupted")
    service.on_turn_completed(turn)
    assert item.status == "waiting"
    assert item.due_at is None
    assert "review the outcome" in item.notes
    assert service.tick()["status"] == "idle"


def test_cancelling_queued_activation_preserves_its_responsibility(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    item = AssistantAgendaItem(title="Review application", due_at=datetime(2026, 10, 16, tzinfo=UTC))
    db.add(item)
    db.commit()
    result = AssistantActivationService(db).tick()
    turn = ActSessionService(db, agent_id="assistant").cancel_turn(result["session_id"], result["turn_id"])
    assert turn.status == "cancelled"
    assert item.status == "waiting"
    assert item.due_at is None


def test_restart_recovers_claimed_agenda_without_replaying_work(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    item = AssistantAgendaItem(title="Verify submission", due_at=datetime(2026, 10, 16, tzinfo=UTC))
    db.add(item)
    db.commit()
    result = AssistantActivationService(db).tick()
    turn = db.get(ActTurn, result["turn_id"])
    turn.status = "running"
    db.commit()
    monkeypatch.setattr("app.services.act_turn_dispatcher.SessionLocal", sessionmaker(bind=db.get_bind()))
    assert AssistantTurnDispatcher().recover() == 1
    db.expire_all()
    assert db.get(ActTurn, turn.id).status == "interrupted"
    assert db.get(AssistantAgendaItem, item.id).status == "waiting"
    assert db.get(AssistantAgendaItem, item.id).due_at is None
    assert AssistantActivationService(db).tick()["status"] == "idle"


def test_interrupted_outcome_evaluation_retains_evidence_as_waiting_work(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    source = ActSessionService(db, agent_id="assistant").create_session()
    proposal = AgentProposal(source_session_id=source.id, title="Submit application", rationale="Requested",
                             instruction="Submit", actions="Submit", fingerprint="submission",
                             execution_status="succeeded", execution_state_json={"receipt": "confirmed"})
    db.add(proposal)
    db.commit()
    service = AssistantActivationService(db)
    result = service.tick()
    turn = finish(db, result["turn_id"], "interrupted")
    service.on_turn_completed(turn)
    service.on_turn_completed(turn)
    items = list(db.scalars(select(AssistantAgendaItem)))
    assert len(items) == 1
    assert items[0].status == "waiting"
    assert "confirmed" in items[0].notes
    assert items[0].due_at is None
    assert service.tick()["status"] == "idle"


def test_fresh_understanding_and_activation_are_supplied_as_data(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    result = activate(db)
    summary = "Chose research for intellectual independence."
    AgentMemoryService(db).write_daily_summary("2026-10-15", summary, source_turn_id=42)
    turn = db.get(ActTurn, result["turn_id"])
    turn.status = "running"
    db.commit()
    message = AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)
    assert "untrusted stored data" in message
    assert summary in message
    assert '"kind": "agenda"' in message


def test_empty_background_result_is_delivered_silently(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    result = activate(db)
    turn = finish(db, result["turn_id"])
    turn.assistant_message = ""
    turn.delivery_status = "pending"
    turn.delivery_connection_id = 999
    turn.delivery_chat_id = "user"
    db.commit()
    deliver_agent_turn_result(turn.id, session_factory=sessionmaker(bind=db.get_bind()))
    db.refresh(turn)
    assert turn.delivery_status == "delivered"


def test_activation_snapshots_direct_chat_without_creating_a_topic(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    connection = TelegramBotConnection(role="assistant_agent", is_default=True, secret_store_id="fake",
        secret_reference="fake", bot_id="1", status="connected", paired_chat_id="123", paired_user_id="456")
    db.add(connection)
    db.commit()
    factory = sessionmaker(bind=db.get_bind())
    def synchronize(service, session):
        raise AssertionError("Assistant activation must not create or synchronize a topic")

    monkeypatch.setattr(ActSessionService, "synchronize_telegram", synchronize)
    result = activate(db)
    with factory() as observer:
        turn = observer.get(ActTurn, result["turn_id"])
        assert turn.status == "queued"
        assert turn.delivery_status == "pending"
        assert turn.delivery_connection_id == connection.id
        assert turn.delivery_chat_id == "123"
        assert turn.delivery_message_thread_id is None
        assert list(observer.scalars(select(TelegramTopicSession))) == []


def test_delivery_claim_prevents_overlapping_duplicate_sends(db, monkeypatch):
    session = ActSessionService(db, agent_id="assistant").create_session()
    turn = ActTurn(session_id=session.id, user_message="Hello", assistant_message="Hi", status="succeeded",
                   delivery_provider="telegram", delivery_connection_id=999, delivery_chat_id="123",
                   delivery_status="pending")
    db.add(turn)
    db.commit()
    factory = sessionmaker(bind=db.get_bind())
    sends = []

    def send(delivery_db, current, _session, reply):
        assert reply == "Hi"
        sends.append(current.id)
        if len(sends) == 1:
            deliver_agent_turn_result(current.id, session_factory=factory)
        current.delivery_status = "delivered"
        delivery_db.commit()

    monkeypatch.setattr("app.services.agent_turn_delivery._deliver_telegram", send)
    deliver_agent_turn_result(turn.id, session_factory=factory)
    assert sends == [turn.id]
    db.refresh(turn)
    assert turn.delivery_status == "delivered"


def test_restart_recovers_terminal_pending_and_never_replays_uncertain_sends(db, monkeypatch):
    session = ActSessionService(db, agent_id="assistant").create_session()
    pending = ActTurn(session_id=session.id, user_message="", assistant_message="Prepared", status="succeeded",
                      delivery_status="pending", delivery_connection_id=999, delivery_chat_id="123")
    uncertain = ActTurn(session_id=session.id, user_message="", assistant_message="Sent?", status="succeeded",
                        delivery_status="sending", delivery_connection_id=999, delivery_chat_id="123")
    db.add_all([pending, uncertain])
    db.commit()
    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.services.act_turn_dispatcher.SessionLocal", factory)
    sent = []

    def deliver(turn_id):
        sent.append(turn_id)
        with factory() as other:
            other.get(ActTurn, turn_id).delivery_status = "delivered"
            other.commit()

    dispatcher = AssistantTurnDispatcher()
    monkeypatch.setattr(dispatcher, "_deliver", deliver)
    dispatcher.recover()
    dispatcher.recover()
    assert sent == [pending.id]
    db.refresh(uncertain)
    assert uncertain.delivery_status == "failed"


@pytest.mark.parametrize("payload", [
    {"daily_time": "9:00"}, {"daily_time": "09:00:00"}, {"timezone": "unknown"},
    {"enabled": "yes"}, {"unexpected": 1},
])
def test_invalid_configuration_is_rejected(db, payload):
    with pytest.raises(ValueError):
        AssistantActivationService(db).configure(payload)


def test_initialization_activates_before_daily_time_and_user_work_keeps_priority(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 12, tzinfo=UTC))  # 08:00 local, before daily review.
    monkeypatch.setattr("app.services.assistant_initialization.AssistantInitializationService.ready", lambda *_: True)
    service = AssistantActivationService(db)
    first = service.tick()
    assert first["kind"] == "initialization"
    turn = db.get(ActTurn, first["turn_id"])
    assert turn.assistant_context_json["initialize_atlas"] is True
    assert "daily_review" not in turn.assistant_context_json
    user = ActSessionService(db, agent_id="assistant").enqueue_turn(first["session_id"], "Help me with something")
    finish(db, turn.id)
    assert service.tick()["status"] == "busy"
    finish(db, user.id)
    assert service.tick()["kind"] == "initialization"
    finish(db, db.scalar(select(ActTurn.id).where(ActTurn.status == "queued")))
    monkeypatch.setattr("app.services.assistant_initialization.AssistantInitializationService.ready", lambda *_: False)
    assert service.tick()["status"] == "idle"


def test_initialization_turn_does_not_count_compact_baseline_as_full_reading(db, monkeypatch):
    session = ActSessionService(db, agent_id="assistant").create_session()
    turn = ActTurn(session_id=session.id, user_message="Review", status="running",
                   assistant_context_json={"kind": "initialization", "initialize_atlas": True})
    db.add(turn)
    db.commit()

    def unexpected(*args, **kwargs):
        raise AssertionError("Initialization must use the full-content reader")

    monkeypatch.setattr("app.services.assistant_changes.AssistantChangeService.read", unexpected)
    prompt = AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)
    assert '"initialize_atlas": true' in prompt
    assert turn.assistant_review_json is None


def test_recurring_notes_are_the_only_task_prompt_and_occur_once(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    notes = "Review changes and retain unresolved responsibilities: UNIQUE_TASK_NOTES."
    item = AssistantService(db).create_item(AgendaCreate(title="User-defined routine", notes=notes,
        recurrence={"frequency": "daily", "time": "09:00", "timezone": "America/Toronto"}))
    result = AssistantActivationService(db).run_now(item["id"], item["revision"])
    turn = db.get(ActTurn, result["turn_id"])
    assert turn.user_message == notes
    assert "notes" not in turn.assistant_context_json["agenda"][0]

    prompt = AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)
    assert prompt.endswith("</assistant_context>\n\n" + notes)
    assert prompt.count(notes) == 1
    data = json.loads(prompt.split("<assistant_context>")[1].split("</assistant_context>")[0])
    assert data["activation"]["agenda"][0]["revision"] == item["revision"]
    assert data["activation"]["occurrence"]["timezone"] == "America/Toronto"
    assert data["activation"]["occurrence"]["previous_turn_id"] is None


def test_queued_recurring_turn_uses_edited_notes_including_legacy_wrappers(db, monkeypatch):
    from app.schemas.assistant import AgendaUpdate

    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    service = AssistantService(db)
    item = service.create_item(AgendaCreate(title="Editable routine", notes="OLD_INSTRUCTIONS",
        recurrence={"frequency": "daily", "time": "09:00", "timezone": "America/Toronto"}))
    result = AssistantActivationService(db).run_now(item["id"], item["revision"])
    turn = db.get(ActTurn, result["turn_id"])
    # Previously queued turns can still contain the former wrapper and snapshot.
    turn.user_message = "LEGACY_WRAPPER\n\nOLD_INSTRUCTIONS"
    turn.assistant_context_json = {**turn.assistant_context_json, "agenda": [{
        **turn.assistant_context_json["agenda"][0], "notes": "OLD_INSTRUCTIONS"}]}
    db.commit()
    updated = service.update_item(item["id"], AgendaUpdate(expected_revision=item["revision"],
                                                         notes="NEW_EDITABLE_INSTRUCTIONS"))
    prompt = AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)
    assert prompt.count("NEW_EDITABLE_INSTRUCTIONS") == 1
    assert "OLD_INSTRUCTIONS" not in prompt and "LEGACY_WRAPPER" not in prompt
    data = json.loads(prompt.split("<assistant_context>")[1].split("</assistant_context>")[0])
    assert data["activation"]["agenda"][0]["revision"] == updated["revision"]


def test_recurring_title_is_used_when_notes_are_empty(db, monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 16, 14, tzinfo=UTC))
    item = AssistantService(db).create_item(AgendaCreate(title="Check the current agenda",
        recurrence={"frequency": "daily", "time": "09:00", "timezone": "America/Toronto"}))
    result = AssistantActivationService(db).run_now(item["id"], item["revision"])
    turn = db.get(ActTurn, result["turn_id"])
    assert turn.user_message == item["title"]
    assert AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False).endswith(
        "</assistant_context>\n\n" + item["title"])


def test_retry_retains_occurrence_and_previous_attempt_without_task_text_duplication(db, monkeypatch):
    start = datetime(2026, 10, 16, 14, tzinfo=UTC)
    freeze(monkeypatch, start)
    item = AssistantService(db).create_item(AgendaCreate(title="Check actual effects", notes="RETRY_INSTRUCTIONS",
        recurrence={"frequency": "daily", "time": "09:00", "timezone": "America/Toronto"}))
    service = AssistantActivationService(db)
    first = service.run_now(item["id"], item["revision"])
    previous = finish(db, first["turn_id"], "failed")
    service.on_turn_completed(previous)
    freeze(monkeypatch, start + timedelta(seconds=31))
    result = service.tick()
    turn = db.get(ActTurn, result["turn_id"])
    prompt = AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)
    data = json.loads(prompt.split("<assistant_context>")[1].split("</assistant_context>")[0])
    occurrence = data["activation"]["occurrence"]
    assert occurrence["previous_turn_id"] == previous.id
    assert occurrence["scheduled_at"] == previous.assistant_context_json["occurrence"]["scheduled_at"]
    assert occurrence["attempts"] == 2
    assert prompt.count("RETRY_INSTRUCTIONS") == 1


def test_standing_prompt_leaves_review_and_summary_procedures_to_tasks_and_tools():
    from app.services.act_app_server_service import ASSISTANT_INSTRUCTIONS_TEMPLATE
    from app.services.assistant_tools import assistant_private_tool_entries

    instructions = ASSISTANT_INSTRUCTIONS_TEMPLATE
    for phrase in ("Morning planning", "Evening reflection", "assistant.changes.list",
                   "assistant.memory.history.read", "assistant.memory.summary.write"):
        assert phrase not in instructions
    assert "previous_turn_id" in instructions and "scheduled_at" in instructions
    tools = {entry["id"]: entry for entry in assistant_private_tool_entries()}
    assert "unavailable sources preserve previous state" in tools["assistant.changes.list"]["description"]
    assert "untrusted evidence" in tools["assistant.memory.history.read"]["description"]
    assert tools["assistant.memory.summary.write"]["input_schema"]["properties"]["summary"]["maxLength"] == 1200
