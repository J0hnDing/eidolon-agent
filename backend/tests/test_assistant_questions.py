from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import ActLiveMessage, ActSession, ActTurn, AssistantAgendaItem, AssistantState
from app.schemas.assistant import AgendaCreate, AgendaUpdate
from app.services.act_session_service import ActSessionService
from app.services.assistant_activation_service import AssistantActivationService
from app.services.assistant_questions import AssistantQuestionService
from app.services.assistant_service import AssistantService
from app.services.function_catalog_service import FunctionCatalogService


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'questions.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda *_a, **_k: [])
    monkeypatch.setattr("app.services.assistant_initialization.AssistantInitializationService.ready", lambda *_: False)
    with Session(engine) as session:
        # Recurrence/default task migration has dedicated coverage.
        AssistantService(session).state()
        session.execute(delete(AssistantAgendaItem))
        session.commit()
        yield session
    engine.dispose()


def freeze(monkeypatch, local_time):
    instant = datetime.fromisoformat(local_time).replace(tzinfo=ZoneInfo("America/Toronto")).astimezone(UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    for module in ("assistant_questions", "assistant_service", "assistant_activation_service", "act_chat_message_service"):
        monkeypatch.setattr(f"app.services.{module}.datetime", Frozen)
    monkeypatch.setattr("app.models.entities.datetime", Frozen)
    return instant


def add_questions(db, count=3, *, due_at=None):
    service = AssistantService(db)
    return [service.create_item(AgendaCreate(kind="question", title=f"What matters about project {i}?", due_at=due_at))
            for i in range(count)]


def running_turn(db):
    session = ActSession(agent_id="assistant", codex_thread_id=f"pending:{uuid4()}")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Review", status="running",
                   assistant_context_json={"kind": "agenda"})
    db.add(turn)
    db.commit()
    return turn


def ask(db, turn, item_id):
    item = db.get(AssistantAgendaItem, item_id)
    return AssistantQuestionService(db).ask(item.id, item.title, item.revision,
        session_id=turn.session_id, turn_id=turn.id)


def test_unscheduled_common_questions_are_not_assigned_daily_slots(db, monkeypatch):
    freeze(monkeypatch, "2026-10-16T09:00:00")
    items = add_questions(db, 4)
    result = AssistantActivationService(db).tick()
    assert all(db.get(AssistantAgendaItem, item["id"]).due_at is None for item in items)
    assert result["status"] == "idle"
    status = AssistantQuestionService(db).status(db.get(AssistantState, 1), datetime.now(UTC))
    assert status["asked_today"] == 0 and "daily_limit" not in status


def test_due_questions_use_the_same_agenda_as_tasks_and_ask_only_once(db, monkeypatch):
    now = freeze(monkeypatch, "2026-10-16T11:00:00")
    items = add_questions(db, due_at=now)
    task = AssistantService(db).create_item(AgendaCreate(title="Prepare interview notes", due_at=now))
    result = AssistantActivationService(db).tick()
    turn = db.get(ActTurn, result["turn_id"])
    agenda = turn.assistant_context_json["agenda"]
    assert {item["id"] for item in agenda} == {item["id"] for item in items} | {task["id"]}
    assert "question_id" not in turn.assistant_context_json
    assert all(item["question_category"] == "common" for item in agenda)
    turn.status = "running"
    db.commit()
    sent = ask(db, turn, items[0]["id"])
    retry = ask(db, turn, items[0]["id"])
    assert sent["asked_today"] == 1 and retry["status"] == "already_asked"
    assert retry["message_id"] == sent["message_id"]
    message = db.get(ActLiveMessage, sent["message_id"])
    assert message.agenda_question_id == items[0]["id"] and message.reply_status == "none"
    first = db.get(AssistantAgendaItem, items[0]["id"])
    assert first.status == "waiting" and first.due_at is None


@pytest.mark.parametrize("category", ["important", "temporal"])
def test_immediate_questions_activate_outside_former_window(db, monkeypatch, category):
    now = freeze(monkeypatch, "2026-10-16T07:30:00")
    service = AssistantService(db)
    item = service.create_item(AgendaCreate(kind="question", question_category=category,
        title="How are you feeling before the interview?", notes="The interview starts in 30 minutes."))
    assert datetime.fromisoformat(item["due_at"]) == now
    result = AssistantActivationService(db).tick()
    turn = db.get(ActTurn, result["turn_id"])
    assert turn.assistant_context_json["agenda"][0]["question_category"] == category
    turn.status = "running"
    db.commit()
    assert ask(db, turn, item["id"])["status"] == "sent"


def test_common_questions_have_no_hard_quota_or_spacing(db, monkeypatch):
    freeze(monkeypatch, "2026-10-16T23:30:00")
    items = add_questions(db, 4)
    turn = running_turn(db)
    for index, item in enumerate(items, 1):
        result = ask(db, turn, item["id"])
        assert result["asked_today"] == index and "daily_limit" not in result
    for category in ("important", "temporal"):
        item = AssistantService(db).create_item(AgendaCreate(kind="question", question_category=category,
            title=f"Immediate {category} question"))
        assert ask(db, turn, item["id"])["status"] == "sent"
    assert len(list(db.scalars(select(ActLiveMessage)))) == 6


@pytest.mark.parametrize("category", ["common", "important", "temporal"])
def test_future_questions_keep_exact_agenda_time(db, monkeypatch, category):
    now = freeze(monkeypatch, "2026-10-16T07:00:00")
    # Ask 30 minutes before an 08:00 interview, without daily slots/window shifts.
    due = now + timedelta(minutes=30)
    item = AssistantService(db).create_item(AgendaCreate(kind="question", question_category=category,
        title="Ready for the interview?", due_at=due))
    turn = running_turn(db)
    with pytest.raises(ValueError, match="future agenda time"):
        ask(db, turn, item["id"])
    db.rollback()
    assert db.get(AssistantAgendaItem, item["id"]).due_at == due.replace(tzinfo=None)
    freeze(monkeypatch, "2026-10-16T07:30:00")
    assert ask(db, turn, item["id"])["status"] == "sent"


def test_waiting_receipt_survives_restart_and_day_rollover(db, monkeypatch):
    freeze(monkeypatch, "2026-10-16T11:00:00")
    item_id = add_questions(db, 1)[0]["id"]
    turn = running_turn(db)
    first = ask(db, turn, item_id)
    now = freeze(monkeypatch, "2026-10-17T11:00:00")
    with Session(db.bind) as restarted:
        retry = ask(restarted, restarted.get(ActTurn, turn.id), item_id)
        assert retry["status"] == "already_asked" and retry["message_id"] == first["message_id"]
        status = AssistantQuestionService(restarted).status(restarted.get(AssistantState, 1), now)
        assert status["asked_today"] == 0 and status["last_asked_at"] is not None
    assert len(list(db.scalars(select(ActLiveMessage)))) == 1


def test_temporal_question_activates_at_exact_agenda_time(db, monkeypatch):
    now = freeze(monkeypatch, "2026-10-16T07:00:00")
    service = AssistantService(db)
    service.state().last_daily_at = now
    db.commit()
    item = service.create_item(AgendaCreate(kind="question", question_category="temporal",
        title="How are you feeling before the interview?", due_at=now + timedelta(minutes=30)))
    assert AssistantActivationService(db).tick()["status"] == "idle"
    freeze(monkeypatch, "2026-10-16T07:29:59")
    assert AssistantActivationService(db).tick()["status"] == "idle"
    freeze(monkeypatch, "2026-10-16T07:30:00")
    result = AssistantActivationService(db).tick()
    turn = db.get(ActTurn, result["turn_id"])
    assert result["kind"] == "agenda" and turn.assistant_context_json["agenda"][0]["id"] == item["id"]
    turn.status = "running"
    db.commit()
    assert ask(db, turn, item["id"])["status"] == "sent"


def test_explicit_reopening_allows_another_ask_without_day_quota(db, monkeypatch):
    freeze(monkeypatch, "2026-10-16T11:00:00")
    item_id = add_questions(db, 1)[0]["id"]
    turn = running_turn(db)
    first = ask(db, turn, item_id)
    current = db.get(AssistantAgendaItem, item_id)
    AssistantService(db).update_item(item_id, AgendaUpdate(expected_revision=current.revision,
        status="open", notes="User explicitly requested asking again."))
    second = ask(db, turn, item_id)
    assert second["message_id"] != first["message_id"] and second["asked_today"] == 2


def test_closed_question_or_stale_revision_cannot_be_asked(db, monkeypatch):
    freeze(monkeypatch, "2026-10-16T11:00:00")
    item_id = add_questions(db, 1)[0]["id"]
    turn = running_turn(db)
    item = db.get(AssistantAgendaItem, item_id)
    with pytest.raises(ValueError, match="changed"):
        AssistantQuestionService(db).ask(item_id, "Why?", item.revision + 5,
            session_id=turn.session_id, turn_id=turn.id)
    db.rollback()
    item.status = "dismissed"
    db.commit()
    with pytest.raises(ValueError, match="no longer open"):
        ask(db, turn, item_id)


def test_future_question_date_is_preserved_even_when_reopening_an_asked_question(db, monkeypatch):
    now = freeze(monkeypatch, "2026-10-16T11:00:00")
    item_id = add_questions(db, 1)[0]["id"]
    turn = running_turn(db)
    ask(db, turn, item_id)
    item = db.get(AssistantAgendaItem, item_id)
    future = now + timedelta(days=7)
    AssistantService(db).update_item(item_id, AgendaUpdate(expected_revision=item.revision, status="open", due_at=future))
    with pytest.raises(ValueError, match="future agenda time"):
        ask(db, turn, item_id)
    assert item.due_at == future.replace(tzinfo=None)


def test_simultaneous_asks_of_same_question_create_one_message(db, monkeypatch):
    freeze(monkeypatch, "2026-10-16T11:00:00")
    item_id = add_questions(db, 1)[0]["id"]
    first, second = running_turn(db), running_turn(db)
    factory = sessionmaker(bind=db.bind)

    def send(turn_id):
        with factory() as other:
            return ask(other, other.get(ActTurn, turn_id), item_id)["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, (first.id, second.id)))
    assert sorted(results) == ["already_asked", "sent"]
    assert len(list(db.scalars(select(ActLiveMessage)))) == 1


def test_uncertain_telegram_send_keeps_receipt_and_never_replays(db, monkeypatch):
    freeze(monkeypatch, "2026-10-16T11:00:00")
    item_id = add_questions(db, 1)[0]["id"]
    turn = running_turn(db)
    turn.delivery_provider, turn.delivery_chat_id = "telegram", "123"
    db.commit()
    sends = []

    def fail(*_a, **_k):
        sends.append(True)
        raise RuntimeError("Provider outcome uncertain")

    monkeypatch.setattr("app.services.telegram_service.TelegramService.send_act_chat_message", fail)
    with pytest.raises(RuntimeError, match="uncertain"):
        ask(db, turn, item_id)
    assert ask(db, turn, item_id)["status"] == "already_asked"
    message = db.scalar(select(ActLiveMessage))
    assert message.reply_status == "failed" and len(sends) == 1
    assert db.get(AssistantAgendaItem, item_id).status == "waiting"


def test_unsent_question_survives_failed_activation_and_retries_later(db, monkeypatch):
    now = freeze(monkeypatch, "2026-10-16T11:00:00")
    item_id = add_questions(db, 1, due_at=now)[0]["id"]
    result = AssistantActivationService(db).tick()
    turn = db.get(ActTurn, result["turn_id"])
    turn.status, turn.completed_at = "failed", now
    db.commit()
    AssistantActivationService(db).on_turn_completed(turn)
    item = db.get(AssistantAgendaItem, item_id)
    assert item.status == "open" and item.due_at == (now + timedelta(minutes=15)).replace(tzinfo=None)
    revision = item.revision
    AssistantActivationService(db).on_turn_completed(turn)
    assert item.revision == revision
    assert AssistantActivationService(db).tick()["status"] == "idle"
    freeze(monkeypatch, "2026-10-16T11:16:00")
    retry = AssistantActivationService(db).tick()
    assert db.get(ActTurn, retry["turn_id"]).assistant_context_json["agenda"][0]["id"] == item_id


def test_question_title_duplicates_reuse_existing_item_across_notes_and_timing(db):
    service = AssistantService(db)
    first = service.create_item(AgendaCreate(kind="question", title="Why research?", notes="Original uncertainty"))
    second = service.create_item(AgendaCreate(kind="question", title="  WHY   research! ", notes="Other wording context",
        due_at=datetime(2026, 10, 20, tzinfo=UTC)))
    assert second["id"] == first["id"] and second["notes"] == first["notes"]
    another = service.create_item(AgendaCreate(kind="question", title="Why engineering?"))
    with pytest.raises(ValueError, match="overlapping"):
        service.update_item(another["id"], AgendaUpdate(expected_revision=1, title="Why research"))


def test_replacement_after_deleting_only_question_has_new_identity(db, monkeypatch):
    freeze(monkeypatch, "2026-10-16T11:00:00")
    original_id = add_questions(db, 1)[0]["id"]
    turn = running_turn(db)
    ask(db, turn, original_id)
    original = db.get(AssistantAgendaItem, original_id)
    AssistantService(db).delete_item(original_id, original.revision)
    replacement_id = add_questions(db, 1)[0]["id"]
    assert replacement_id > original_id
    freeze(monkeypatch, "2026-10-16T17:00:00")
    assert ask(db, turn, replacement_id)["asked_today"] == 2


def test_concurrent_duplicate_question_creates_return_one_item(db):
    factory = sessionmaker(bind=db.bind)

    def create(_):
        with factory() as other:
            return AssistantService(other).create_item(AgendaCreate(kind="question", title="Why research?"))["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    assert ids[0] == ids[1]
    assert len(list(db.scalars(select(AssistantAgendaItem)))) == 1


def test_private_question_asking_and_answer_save_use_authenticated_tools(db, monkeypatch):
    from app.services.agent_policy_service import AgentPolicyService
    from app.services.mcp_function_service import McpFunctionService

    freeze(monkeypatch, "2026-10-16T11:00:00")
    item_id = add_questions(db, 1)[0]["id"]
    AssistantService(db).create_item(AgendaCreate(title="Prepare notes"))
    turn = running_turn(db)
    token = AgentPolicyService(db).issue("assistant", turn.session_id)
    mcp = McpFunctionService(db, agent_token=token)
    list_name = mcp.tool_name("agent_private", "assistant.agenda.list")
    questions = mcp.invoke(list_name, {"kind": "question", "active_only": False}).output["items"]
    assert [q["id"] for q in questions] == [item_id]
    ask_name = mcp.tool_name("agent_private", "assistant.question.ask")
    result = mcp.invoke(ask_name, {"id": item_id, "text": "What matters most about that project?",
        "expected_revision": questions[0]["revision"]})
    assert result.output["asked_today"] == 1
    assert ask_name not in {tool.name for tool in McpFunctionService(db).list_tools()}
    turn.status = "succeeded"
    db.commit()
    answer_turn = ActTurn(session_id=turn.session_id, user_message="Intellectual independence.", status="running")
    db.add(answer_turn)
    db.commit()
    memory = mcp.invoke(mcp.tool_name("agent_private", "assistant.remember"), {
        "key": "project_motivation", "value": "Intellectual independence",
        "attribution": "stated", "evidence": "User answered the project motivation question in this conversation.",
    }).output
    current = db.get(AssistantAgendaItem, item_id)
    mcp.invoke(mcp.tool_name("agent_private", "assistant.agenda.update"), {
        "id": item_id, "expected_revision": current.revision, "status": "done",
        "notes": f"Answered: intellectual independence. Saved to fact {memory['id']}; evidence user answer.",
    })
    assert db.get(AssistantAgendaItem, item_id).status == "done"
    assert memory["category"] == "facts"
    assert f"fact {memory['id']}" in db.get(AssistantAgendaItem, item_id).notes


def test_question_receipt_schema_upgrade_is_idempotent(db, monkeypatch):
    from sqlalchemy import text

    from app import db as db_module

    with db.bind.begin() as connection:
        connection.execute(text("ALTER TABLE act_live_messages DROP COLUMN agenda_question_id"))
        connection.execute(text("ALTER TABLE assistant_agenda DROP COLUMN question_category"))
    monkeypatch.setattr(db_module, "engine", db.bind)
    db_module.ensure_local_schema()
    db_module.ensure_local_schema()
    assert "agenda_question_id" in {c["name"] for c in inspect(db.bind).get_columns("act_live_messages")}
    assert db.get(AssistantState, 1).summary == ""


def test_category_schema_validation_and_promotion_to_immediate(db, monkeypatch):
    from pydantic import ValidationError

    now = freeze(monkeypatch, "2026-10-16T06:00:00")
    service = AssistantService(db)
    item = service.create_item(AgendaCreate(kind="question", title="Which option?"))
    assert item["question_category"] == "common" and item["due_at"] is None
    changed = service.update_item(item["id"], AgendaUpdate(expected_revision=item["revision"],
        question_category="important"))
    assert changed["question_category"] == "important"
    assert datetime.fromisoformat(changed["due_at"]) == now
    for payload in ({"question_category": "urgent"}, {"question_category": None}):
        with pytest.raises(ValidationError):
            AgendaCreate(kind="question", title="Why?", **payload)
        with pytest.raises(ValidationError):
            AgendaUpdate(expected_revision=1, **payload)


def test_old_question_category_migration_preserves_existing_schedule(db, monkeypatch):
    from sqlalchemy import text

    from app import db as db_module

    due = datetime(2026, 10, 20, 11, tzinfo=UTC)
    item = AssistantService(db).create_item(AgendaCreate(kind="question", title="Why?", due_at=due))
    with db.bind.begin() as connection:
        connection.execute(text("ALTER TABLE assistant_agenda DROP COLUMN question_category"))
    monkeypatch.setattr(db_module, "engine", db.bind)
    db_module.ensure_local_schema()
    db_module.ensure_local_schema()
    db.expire_all()
    restored = db.get(AssistantAgendaItem, item["id"])
    assert restored.question_category == "common" and restored.due_at == due.replace(tzinfo=None)
