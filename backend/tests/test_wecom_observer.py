from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    ActSession,
    ActTelegramBinding,
    ActTurn,
    IntegrationConnection,
    TelegramBotConnection,
    WeComInboundMessage,
    WeComObserverBinding,
    WeComObserverUserBinding,
)
from app.services.act_session_service import ActSessionService
from app.services.secret_store import FakeSecretStore
from app.services.wecom_provider import (
    WECOM_CALLBACK_COMMAND,
    parse_inbound_message,
)
from app.services.wecom_service import WeComObserverWorker, WeComService


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float | None = None) -> str:
        raise TimeoutError

    def close(self) -> None:
        return


class FakeWorker:
    def __init__(self) -> None:
        self.notifications = 0

    def notify(self) -> None:
        self.notifications += 1


class FakeAppServer:
    class Sessions:
        def archive_thread(self, _thread_id: str) -> None:
            return

    sessions = Sessions()


def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _callback(message_id: str, user_id: str, text: str, *, chat_type: str = "single") -> dict:
    return {
        "cmd": WECOM_CALLBACK_COMMAND,
        "headers": {"req_id": f"callback-{message_id}"},
        "body": {
            "msgid": message_id,
            "chattype": chat_type,
            "from": {"userid": user_id},
            "msgtype": "text",
            "text": {"content": text},
        },
    }


def test_wecom_parser_accepts_transcribed_voice_and_identifies_groups() -> None:
    voice = _callback("voice-1", "user-1", "transcribed text")
    voice["body"]["msgtype"] = "voice"
    voice["body"]["voice"] = {"content": "transcribed text"}
    del voice["body"]["text"]

    parsed = parse_inbound_message(voice)
    assert parsed is not None
    assert parsed.text == "transcribed text"
    group = parse_inbound_message(_callback("group-1", "user-1", "ignored", chat_type="group"))
    assert group is not None
    assert group.chat_type == "group"


def test_wecom_connection_pairing_and_message_deduplication() -> None:
    engine, factory = session_factory()
    secret_store = FakeSecretStore()
    worker_stub = FakeWorker()
    db: Session = factory()
    try:
        result = WeComService(db, secret_store=secret_store, worker=worker_stub).connect("bot-id", "bot-secret")
        connection = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "wecom"))
        assert connection is not None
        assert result.connection.agent_id == "observer"
        assert secret_store.values[connection.secret_reference] == "bot-secret"
        assert result.pairing_code not in secret_store.values.values()

        socket = FakeSocket()
        worker = WeComObserverWorker(session_factory=factory, secret_store=secret_store)
        config = worker._configured_connection()
        assert config is not None
        worker._handle_inbound(socket, config, _callback("pair-1", "user-1", f"/pair {result.pairing_code}"))

        pairing = db.get(WeComObserverBinding, connection.id)
        assert pairing is not None
        assert pairing.pairing_code_hash is None
        user = db.scalar(
            select(WeComObserverUserBinding).where(
                WeComObserverUserBinding.paired_user_id == "user-1"
            )
        )
        assert user is not None

        worker._handle_inbound(socket, config, _callback("message-1", "user-1", "hello"))
        worker._handle_inbound(socket, config, _callback("message-1", "user-1", "hello"))
        turns = list(db.scalars(select(ActTurn)))
        assert len(turns) == 1
        assert turns[0].delivery_provider == "wecom"
        assert turns[0].delivery_chat_id == "user-1"
        assert db.scalar(select(WeComInboundMessage).where(WeComInboundMessage.message_id == "message-1")) is not None
        assert worker_stub.notifications == 1
        assert socket.sent
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def test_wecom_disconnect_preserves_observer_sessions() -> None:
    engine, factory = session_factory()
    secret_store = FakeSecretStore()
    worker_stub = FakeWorker()
    db: Session = factory()
    try:
        service = WeComService(db, secret_store=secret_store, worker=worker_stub)
        result = service.connect("bot-id", "bot-secret")
        observer_session = ActSession(agent_id="observer", codex_thread_id="pending:session", title="Observer")
        db.add(observer_session)
        db.commit()
        connection = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "wecom"))
        assert connection is not None
        binding = WeComObserverUserBinding(
            connection_id=connection.id,
            paired_user_id="user-1",
            active_session_id=observer_session.id,
        )
        db.add(binding)
        db.commit()

        service.remove()

        assert db.get(ActSession, observer_session.id) is not None
        assert db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "wecom")) is None
        assert secret_store.values == {}
        assert worker_stub.notifications == 2
        assert result.connection.bot_id == "bot-id"
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def test_wecom_supports_multiple_users_with_independent_session_pointers() -> None:
    engine, factory = session_factory()
    secret_store = FakeSecretStore()
    worker_stub = FakeWorker()
    db: Session = factory()
    try:
        service = WeComService(db, secret_store=secret_store, worker=worker_stub)
        first_pairing = service.connect("bot-id", "bot-secret")
        connection = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "wecom"))
        assert connection is not None
        socket = FakeSocket()
        worker = WeComObserverWorker(session_factory=factory, secret_store=secret_store)
        config = worker._configured_connection()
        assert config is not None
        worker._handle_inbound(socket, config, _callback("pair-1", "user-1", f"/pair {first_pairing.pairing_code}"))

        second_pairing = service.start_user_pairing()
        assert second_pairing.pairing_code not in secret_store.values.values()
        worker._handle_inbound(socket, config, _callback("pair-2", "user-2", f"/pair {second_pairing.pairing_code}"))

        older = ActSession(agent_id="observer", codex_thread_id="pending:older", title="Older")
        newest = ActSession(agent_id="observer", codex_thread_id="pending:newest", title="Newest")
        db.add_all([older, newest])
        db.commit()
        user_one = db.scalar(
            select(WeComObserverUserBinding).where(WeComObserverUserBinding.paired_user_id == "user-1")
        )
        user_two = db.scalar(
            select(WeComObserverUserBinding).where(WeComObserverUserBinding.paired_user_id == "user-2")
        )
        assert user_one is not None and user_two is not None
        older.status = "archived"
        user_one.active_session_id = older.id
        db.commit()

        worker._handle_inbound(socket, config, _callback("normal-1", "user-1", "hello"))
        db.refresh(user_one)
        assert user_one is not None and user_one.active_session_id == newest.id
        assert user_two.active_session_id is None

        worker._handle_inbound(socket, config, _callback("new-2", "user-2", "/new"))
        db.refresh(user_two)
        assert user_two.active_session_id not in {None, newest.id}
        assert user_one.active_session_id == newest.id

        status = service.connection_status()
        assert {user.user_id for user in status.paired_users} == {"user-1", "user-2"}
        service.remove_user("user-1")
        assert db.get(ActSession, newest.id) is not None
        assert db.scalar(
            select(WeComObserverUserBinding).where(WeComObserverUserBinding.paired_user_id == "user-1")
        ) is None
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def test_wecom_rejects_group_pairing() -> None:
    engine, factory = session_factory()
    secret_store = FakeSecretStore()
    db: Session = factory()
    try:
        result = WeComService(db, secret_store=secret_store, worker=FakeWorker()).connect(
            "bot-id", "bot-secret"
        )
        socket = FakeSocket()
        worker = WeComObserverWorker(session_factory=factory, secret_store=secret_store)
        config = worker._configured_connection()
        assert config is not None
        worker._handle_inbound(
            socket,
            config,
            _callback("group-pair", "user-1", f"/pair {result.pairing_code}", chat_type="group"),
        )
        assert db.query(WeComObserverUserBinding).count() == 0
        assert "private WeCom chat" in socket.sent[-1]
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def test_archiving_session_clears_telegram_and_wecom_pointers() -> None:
    engine, factory = session_factory()
    db: Session = factory()
    try:
        session = ActSession(agent_id="observer", codex_thread_id="pending:observer", title="Observer")
        telegram = TelegramBotConnection(
            role="observer_agent",
            is_default=True,
            secret_store_id="test",
            secret_reference="telegram/test",
            bot_id="1",
            status="connected",
        )
        wecom = IntegrationConnection(
            provider="wecom",
            is_default=True,
            secret_store_id="test",
            secret_reference="wecom/test",
            credential_kind="wecom_bot_secret",
            status="connected",
            account_login="bot",
            account_id="bot",
            bot_id="bot",
            last_validated_at=datetime.now(UTC),
        )
        db.add_all([session, telegram, wecom])
        db.flush()
        db.add_all(
            [
                ActTelegramBinding(connection_id=telegram.id, active_session_id=session.id),
                WeComObserverUserBinding(
                    connection_id=wecom.id,
                    paired_user_id="user-1",
                    active_session_id=session.id,
                ),
            ]
        )
        db.commit()

        ActSessionService(
            db,
            app_server=FakeAppServer(),  # type: ignore[arg-type]
            agent_id="observer",
        ).archive(session.id)

        assert db.get(ActTelegramBinding, telegram.id).active_session_id is None
        wecom_binding = db.scalar(select(WeComObserverUserBinding))
        assert wecom_binding is not None and wecom_binding.active_session_id is None
    finally:
        db.close()
        Base.metadata.drop_all(engine)
