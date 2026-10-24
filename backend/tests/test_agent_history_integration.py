"""Completed transcript ownership across storage, HTTP, recovery and delivery."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, enable_sqlite_foreign_keys, get_db
from app.execution.context_factory import InvocationContextFactory
from app.models import (
    ActLiveMessage,
    ActSession,
    ActTurn,
    AssistantState,
    InvocationApproval,
    TelegramBotConnection,
    TelegramTopicSession,
)
from app.routers.act import router as act_router
from app.routers.agents import router as agents_router
from app.services.act_turn_dispatcher import ActTurnDispatcher
from app.services.agent_chat_history import project_session, reclaim_history_database_space
from app.services.agent_memory_service import AgentMemoryService
from app.services.agent_policy_service import AgentPolicyService
from app.services.agent_turn_delivery import deliver_agent_turn_result
from app.services.assistant_turn_dispatcher import AssistantTurnDispatcher
from app.services.telegram_service import TelegramService


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def client(factory):
    def database():
        with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(act_router)
    app.include_router(agents_router)
    app.dependency_overrides[get_db] = database
    with TestClient(app) as http:
        yield http


def seed_chat(db, agent_id, *, when=None, status="succeeded", activation=None):
    when = when or datetime.now(UTC)
    session = ActSession(agent_id=agent_id, codex_thread_id=f"pending:history-{agent_id}")
    db.add(session)
    db.flush()
    turn = ActTurn(
        session_id=session.id, user_message="User's complete message\nSecond line: 日本語",
        assistant_message="Complete agent answer\n" + "Details. " * 900,
        status=status, created_at=when, completed_at=when if status == "succeeded" else None,
        assistant_context_json=activation,
    )
    db.add(turn)
    db.flush()
    question = ActLiveMessage(turn_id=turn.id, role="assistant", text="Which exact version?",
                              reply_status="answered", created_at=when)
    db.add(question)
    db.flush()
    reply = ActLiveMessage(turn_id=turn.id, role="user", text="Version 2, with all details.",
                           reply_to_message_id=question.id, created_at=when)
    db.add(reply)
    db.commit()
    return session, turn, [question, reply]


def assert_sql_has_no_transcript(db, turn_id):
    assert db.execute(text("SELECT user_message, assistant_message FROM act_turns WHERE id=:id"),
                      {"id": turn_id}).one() == ("", None)
    assert set(db.scalars(select(ActLiveMessage.text).where(ActLiveMessage.turn_id == turn_id))) == {""}


def archive_paths(memory):
    return sorted(memory.memory_root.glob("chat-history/*/*.json"))


@pytest.mark.parametrize("agent_id", ["act", "observer", "assistant"])
def test_completed_history_http_reads_files_without_rehydrating_sql(factory, client, agent_id):
    with factory() as db:
        session, turn, live = seed_chat(db, agent_id)
        expected = (turn.user_message, turn.assistant_message, [row.text for row in live])
        AgentMemoryService(db).sync_chat_history()
        assert turn.history_archived_at is not None
        assert_sql_has_no_transcript(db, turn.id)

        # Both the generic agent API and Act's compatibility API must project
        # complete messages from files, including live questions and replies.
        routes = [f"/agents/{agent_id}/sessions/{session.id}"]
        if agent_id == "act":
            routes.append(f"/act/sessions/{session.id}")
        for route in routes:
            response = client.get(route)
            assert response.status_code == 200, response.text
            displayed = response.json()["turns"][0]
            assert (displayed["user_message"], displayed["assistant_message"]) == expected[:2]
            assert [message["text"] for message in displayed["live_messages"]] == expected[2]

        # A display read followed by an unrelated ORM mutation cannot persist
        # the projected text back into the database.
        projected = project_session(db, session)
        assert projected.turns[0].assistant_message == expected[1]
        session.title = "Renamed after reading history"
        db.commit()
        assert_sql_has_no_transcript(db, turn.id)


@pytest.mark.parametrize("agent_id", ["act", "observer", "assistant"])
def test_manual_file_edit_and_deletion_are_authoritative_for_http(factory, client, agent_id):
    with factory() as db:
        session, turn, _ = seed_chat(db, agent_id)
        memory = AgentMemoryService(db)
        memory.sync_chat_history()
        path = next(path for path in archive_paths(memory)
                    if (payload := json.loads(path.read_text(encoding="utf-8")))["role"] == "assistant"
                    and payload["text"].startswith("Complete agent answer"))
        payload["text"] = "Edited by the user in the canonical history file."
        path.write_text(json.dumps(payload), encoding="utf-8")
        route = f"/agents/{agent_id}/sessions/{session.id}"
        assert client.get(route).json()["turns"][0]["assistant_message"] == payload["text"]

        for path in archive_paths(memory):
            path.unlink()
        assert client.get(route).json()["turns"] == []
        memory.sync_chat_history()
        assert archive_paths(memory) == []
        assert_sql_has_no_transcript(db, turn.id)


@pytest.mark.parametrize("agent_id", ["act", "observer", "assistant"])
def test_retention_discards_completed_content_from_api_and_recovery(factory, client, agent_id):
    old = datetime.now(UTC) - timedelta(days=31)
    with factory() as db:
        session, turn, _ = seed_chat(db, agent_id, when=old)
        memory = AgentMemoryService(db)
        memory.sync_chat_history(now=old)
        assert archive_paths(memory)
        memory.sync_chat_history()
        assert archive_paths(memory) == []
        assert client.get(f"/agents/{agent_id}/sessions/{session.id}").json()["turns"] == []
        current = ActTurn(session_id=session.id, user_message="Continue with this new request", status="queued")
        db.add(current)
        db.commit()
        dispatcher = AssistantTurnDispatcher if agent_id == "assistant" else ActTurnDispatcher
        recovered = dispatcher._turn_input(db, current, recovered_thread=True)
        assert "Continue with this new request" in recovered
        assert "User's complete message" not in recovered
        assert "Complete agent answer" not in recovered
        assert "Which exact version?" not in recovered
        assert_sql_has_no_transcript(db, turn.id)


@pytest.mark.parametrize("agent_id", ["act", "observer", "assistant"])
def test_recovery_reads_retained_files_while_assistant_keeps_fresh_thread_boundary(factory, agent_id):
    with factory() as db:
        session, previous, _ = seed_chat(db, agent_id)
        original_user, original_reply = previous.user_message, previous.assistant_message
        AgentMemoryService(db).sync_chat_history()
        current = ActTurn(session_id=session.id, user_message="A new request", status="queued")
        db.add(current)
        db.commit()
        dispatcher = AssistantTurnDispatcher if agent_id == "assistant" else ActTurnDispatcher
        recovered = dispatcher._turn_input(db, current, recovered_thread=True)
        assert "A new request" in recovered
        if agent_id == "assistant":
            assert original_user not in recovered
            assert original_reply not in recovered
        else:
            assert original_user in recovered
            assert original_reply in recovered
        assert_sql_has_no_transcript(db, previous.id)


def configure_telegram(db, session, turn):
    connection = TelegramBotConnection(
        role=f"{session.agent_id}_agent", secret_store_id="test", secret_reference="test-token",
        bot_id="123", status="connected", paired_chat_id="456", paired_user_id="789",
    )
    db.add(connection)
    db.flush()
    turn.delivery_provider = "telegram"
    turn.delivery_connection_id = connection.id
    turn.delivery_chat_id = "456"
    turn.delivery_status = "pending"
    if session.agent_id != "assistant":
        turn.delivery_message_thread_id = 42
        db.add(TelegramTopicSession(connection_id=connection.id, telegram_chat_id="456",
                                    message_thread_id=42, session_id=session.id))
    db.commit()


@pytest.mark.parametrize("agent_id", ["act", "observer", "assistant"])
@pytest.mark.parametrize("expired", [False, True])
def test_pending_delivery_uses_retained_file_and_never_expired_fallback(factory, monkeypatch, agent_id, expired):
    sent = []
    monkeypatch.setattr(TelegramService, "send_agent_turn_result",
                        lambda _self, _connection, message, **_kwargs: sent.append(message))
    when = datetime.now(UTC) - timedelta(days=31 if expired else 0)
    with factory() as db:
        session, turn, _ = seed_chat(db, agent_id, when=when)
        original_reply = turn.assistant_message
        configure_telegram(db, session, turn)
        memory = AgentMemoryService(db)
        memory.sync_chat_history(now=when)
        assert_sql_has_no_transcript(db, turn.id)
        if expired:
            memory.sync_chat_history()
        deliver_agent_turn_result(turn.id, session_factory=factory)
        db.refresh(turn)
        assert sent == ([] if expired else [original_reply])
        assert turn.delivery_status == ("failed" if expired else "delivered")


@pytest.mark.parametrize("expired", [False, True])
def test_assistant_background_result_delivery_uses_file_only(factory, monkeypatch, expired):
    sent = []
    monkeypatch.setattr(TelegramService, "send_agent_turn_result",
                        lambda _self, _connection, message, **_kwargs: sent.append(message))
    when = datetime.now(UTC) - timedelta(days=31 if expired else 0)
    with factory() as db:
        session, turn, _ = seed_chat(db, "assistant", when=when, activation={"kind": "daily"})
        original_reply = turn.assistant_message
        configure_telegram(db, session, turn)
        memory = AgentMemoryService(db)
        memory.sync_chat_history(now=when)
        assert_sql_has_no_transcript(db, turn.id)
        if expired:
            memory.sync_chat_history()
        deliver_agent_turn_result(turn.id, session_factory=factory)
        db.refresh(turn)
        assert sent == ([] if expired else [original_reply])
        assert turn.delivery_status == ("failed" if expired else "delivered")


def test_successful_silent_background_turn_is_not_reported_as_failed(factory, monkeypatch):
    sent = []
    monkeypatch.setattr(TelegramService, "send_agent_turn_result",
                        lambda _self, _connection, message, **_kwargs: sent.append(message))
    with factory() as db:
        session = ActSession(agent_id="assistant", codex_thread_id="pending:silent-background")
        db.add(session)
        db.flush()
        turn = ActTurn(session_id=session.id, user_message="Internal morning scan",
                       assistant_message="", status="succeeded", completed_at=datetime.now(UTC),
                       assistant_context_json={"kind": "daily"})
        db.add(turn)
        db.commit()
        configure_telegram(db, session, turn)
        AgentMemoryService(db).sync_chat_history()
        assert turn.user_message == ""
        assert turn.assistant_message is None
        deliver_agent_turn_result(turn.id, session_factory=factory)
        db.refresh(turn)
        assert sent == []
        assert turn.delivery_status == "delivered"


@pytest.mark.parametrize("agent_id", ["act", "observer", "assistant"])
def test_running_turn_and_awaiting_live_question_keep_sql_runtime_content(factory, client, agent_id):
    with factory() as db:
        session, turn, live = seed_chat(db, agent_id, status="running")
        live[0].reply_status = "awaiting"
        db.commit()
        original_user, original_question = turn.user_message, live[0].text
        AgentMemoryService(db).sync_chat_history()
        db.refresh(turn)
        db.refresh(live[0])
        assert turn.history_archived_at is None
        assert turn.user_message == original_user
        assert live[0].text == original_question
        assert live[0].reply_status == "awaiting"
        displayed = client.get(f"/agents/{agent_id}/sessions/{session.id}").json()["turns"][0]
        assert displayed["status"] == "running"
        assert displayed["user_message"] == original_user
        assert displayed["live_messages"][0]["text"] == original_question


def test_pending_approval_can_resume_active_turn_after_history_sync(factory):
    with factory() as db:
        session, turn, _ = seed_chat(db, "assistant", status="running")
        token = AgentPolicyService(db).issue("assistant", session.id)
        context_factory = InvocationContextFactory(db)
        context = context_factory.from_agent_credential(token)
        approval = InvocationApproval(
            target_kind="integration", target_id="calendar.create", target_contract_fingerprint="contract-v1",
            target_description="Create the requested event", caller_type="agent", source="agent_mcp",
            input_json={"title": "An event"}, input_hash="test-hash", reason_to_call="User requested it",
            dispatch_metadata_json={"invocation_context_v1": context.serialize()},
        )
        db.add(approval)
        db.commit()
        original = turn.user_message
        AgentMemoryService(db).sync_chat_history()
        restored = context_factory.from_approval(approval)
        assert restored.agent_id == "assistant"
        assert restored.agent_session_id == session.id
        assert restored.agent_turn_id == turn.id
        assert db.get(ActTurn, restored.agent_turn_id).user_message == original
        assert turn.history_archived_at is None


def test_startup_compaction_reclaims_erased_sql_pages_and_preserves_full_archive(tmp_path):
    database_path = tmp_path / "history-test.sqlite"
    engine = create_engine(f"sqlite:///{database_path}")
    event.listen(engine, "connect", enable_sqlite_foreign_keys)
    Base.metadata.create_all(engine)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA secure_delete").scalar_one() == 1
        with sessionmaker(bind=engine)() as db:
            _session, turn, _live = seed_chat(db, "observer")
            # SQLite overflow pages make the physical reclamation observable.
            # The complete body must remain available in the canonical file.
            full_reply = "unique completed transcript\n" + "Full paragraph without truncation.\n" * 35000
            turn.assistant_message = full_reply
            db.commit()
            original_size = database_path.stat().st_size
            memory = AgentMemoryService(db)
            memory.sync_chat_history()
            assert_sql_has_no_transcript(db, turn.id)
            assert memory.read_turn_content(turn)["assistant_message"] == full_reply
        reclaim_history_database_space(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA freelist_count").scalar_one() == 0
        assert database_path.stat().st_size < original_size
    finally:
        engine.dispose()


def test_periodic_retention_discards_history_while_assistant_is_disabled(factory, monkeypatch):
    from app import main

    old = datetime.now(UTC) - timedelta(days=31)
    with factory() as db:
        _session, turn, _ = seed_chat(db, "observer", when=old)
        db.add(AssistantState(enabled=False))
        db.commit()
        memory = AgentMemoryService(db)
        memory.sync_chat_history(now=old)
        assert archive_paths(memory)
        monkeypatch.setattr(main, "SessionLocal", factory)
        # No HTTP history read, incoming chat or proactive Assistant activation.
        main.maintain_agent_chat_history()
        assert archive_paths(memory) == []
        assert_sql_has_no_transcript(db, turn.id)
