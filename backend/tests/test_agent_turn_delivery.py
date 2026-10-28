from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ActSession, ActTurn
from app.services.agent_memory_service import AgentMemoryService
from app.services.agent_turn_delivery import deliver_agent_turn_result
from app.services.secret_store import FakeSecretStore
from app.services.telegram_provider import FakeTelegramBotApi, PairingMessage, TelegramProviderError
from app.services.telegram_service import TELEGRAM_ASSISTANT_ROLE, TelegramService


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'delivery.db'}")
    Base.metadata.create_all(engine)
    store = FakeSecretStore()
    api = FakeTelegramBotApi()
    factory = sessionmaker(bind=engine)
    with factory() as db:
        service = TelegramService(db, secret_store=store, api_factory=lambda _: api, role=TELEGRAM_ASSISTANT_ROLE)
        pairing = service.start_pairing("123:private-token")
        connection = service._connection()
        service._handle_pairing(connection.id, connection.bot_id,
                                PairingMessage(update_id=1, code=pairing.pairing_code, chat_id=33, user_id=44))
        session = ActSession(agent_id="assistant", origin="web", title="Assistant", status="active",
                             codex_thread_id="test-thread")
        db.add(session)
        db.flush()
        turn = ActTurn(session_id=session.id, user_message="", assistant_message="Prepared **result**.",
                       status="succeeded", completed_at=datetime.now(UTC),
                       assistant_context_json={"kind": "agenda"}, delivery_provider="telegram",
                       delivery_connection_id=connection.id, delivery_chat_id="33", delivery_status="pending")
        db.add(turn)
        db.commit()
        AgentMemoryService(db).sync_chat_history()
        db.refresh(turn)
        assert turn.history_archived_at is not None
        assert turn.assistant_message is None
        monkeypatch.setattr("app.services.agent_turn_delivery.TelegramService",
                            lambda delivery_db, role: TelegramService(
                                delivery_db, role=role, secret_store=store, api_factory=lambda _: api))
        yield db, factory, turn, service, connection, api
    engine.dispose()


@pytest.mark.parametrize("error_type", ["rate_limited", "connection_failed"])
def test_archived_result_retries_unsent_chunk_without_replaying_accepted_chunks(delivery, monkeypatch, error_type):
    db, factory, turn, _, _, api = delivery
    monkeypatch.setattr("app.services.telegram_service.render_agent_message_chunks", lambda _: ["first", "second"])
    original = api.send_message
    attempts, waits = [], []

    def send(chat_id, text, **kwargs):
        attempts.append(text)
        if attempts == ["first", "second"]:
            raise TelegramProviderError(error_type, "Rejected", retry_after_seconds=2)
        return original(chat_id, text, **kwargs)

    monkeypatch.setattr(api, "send_message", send)
    monkeypatch.setattr("app.services.telegram_service.time.sleep", waits.append)
    deliver_agent_turn_result(turn.id, session_factory=factory)
    deliver_agent_turn_result(turn.id, session_factory=factory)
    db.refresh(turn)
    assert turn.delivery_status == "delivered"
    assert attempts == ["first", "second", "second"]
    assert waits == [2]
    assert [message["text"] for message in api.sent_messages] == ["first", "second"]
    assert all(message.get("message_thread_id") is None for message in api.sent_messages)
    assert turn.assistant_message is None


@pytest.mark.parametrize("error_type,delay,attempt_count", [
    ("rate_limited", 1, 3), ("connection_failed", None, 3), ("rate_limited", 31, 1), ("rate_limited", 20, 2),
    ("provider_timeout", None, 1), ("provider_unavailable", None, 1), ("invalid_credential", None, 1),
])
def test_delivery_retry_is_bounded_and_logs_only_sanitized_failure(
    delivery, monkeypatch, caplog, error_type, delay, attempt_count,
):
    db, factory, turn, _, _, api = delivery
    attempts = []

    def send(*args, **kwargs):
        attempts.append(1)
        raise TelegramProviderError(error_type, "sensitive-token-and-body", retry_after_seconds=delay)

    monkeypatch.setattr(api, "send_message", send)
    monkeypatch.setattr("app.services.telegram_service.time.sleep", lambda _: None)
    deliver_agent_turn_result(turn.id, session_factory=factory)
    db.refresh(turn)
    assert turn.delivery_status == "failed"
    assert len(attempts) == attempt_count
    assert f"turn_id={turn.id} error_type={error_type}" in caplog.text
    assert "sensitive-token-and-body" not in caplog.text


def test_delivery_does_not_redirect_after_pairing_changes_during_retry(delivery, monkeypatch):
    db, factory, turn, service, _, api = delivery
    attempts = []

    def send(*args, **kwargs):
        attempts.append(1)
        raise TelegramProviderError("rate_limited", "Rejected", retry_after_seconds=1)

    monkeypatch.setattr(api, "send_message", send)
    monkeypatch.setattr("app.services.telegram_service.time.sleep", lambda _: service.start_pairing("456:replacement"))
    deliver_agent_turn_result(turn.id, session_factory=factory)
    db.refresh(turn)
    assert turn.delivery_status == "failed"
    assert len(attempts) == 1
    assert api.sent_messages == []


@pytest.mark.parametrize("receipt", [{}, {"message_id": True}, {"message_id": 0}])
def test_invalid_telegram_receipt_never_counts_as_delivered(delivery, monkeypatch, receipt):
    db, factory, turn, _, _, api = delivery
    monkeypatch.setattr(api, "send_message", lambda *args, **kwargs: receipt)
    deliver_agent_turn_result(turn.id, session_factory=factory)
    db.refresh(turn)
    assert turn.delivery_status == "failed"
