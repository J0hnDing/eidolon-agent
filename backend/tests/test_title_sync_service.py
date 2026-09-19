from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import ActSession, ActTurn, TelegramBotConnection, TelegramTopicSession
from app.services.act_session_service import ActSessionService
from app.services.act_turn_dispatcher import ActTurnDispatcher
from app.services.agent_policy_service import AgentPolicyService
from app.services.codex_routing_service import CodexRoutingService
from app.services.product_manager_session_service import ProductManagerSessionService
from app.services.secret_store import FakeSecretStore
from app.services.telegram_provider import FakeTelegramBotApi, TelegramProviderError
from app.services.telegram_service import TELEGRAM_ACT_ROLE, TelegramService
from app.services.title_sync_service import TitleSyncService


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


class _FakeCodexSessions:
    def __init__(self, name: str | None) -> None:
        self.name = name
        self.reads: list[str] = []
        self.sets: list[tuple[str, str]] = []

    def read_thread_name(self, thread_id: str) -> str | None:
        self.reads.append(thread_id)
        return self.name

    def set_thread_name(self, thread_id: str, name: str) -> None:
        self.sets.append((thread_id, name))
        self.name = name


class _FakeRuntimeSessions(_FakeCodexSessions):
    def run_structured_turn(self, thread_id: str, _input: str, _schema: dict, **kwargs):
        on_turn_started = kwargs.get("on_turn_started")
        if on_turn_started is not None:
            on_turn_started("codex-turn-1")
        return SimpleNamespace(output_text='{"response":"completed"}', items=[])


class _FakeAppServer:
    def __init__(self, sessions: _FakeRuntimeSessions) -> None:
        self.sessions = sessions

    def start_thread(self, _db: Session, **_kwargs: object) -> str:
        return "codex-thread-1"

    def resume_thread(self, _db: Session, _thread_id: str, **_kwargs: object) -> None:
        return None


def _make_session(
    db: Session,
    *,
    agent_id: str = "act",
    origin: str = "web",
    title: str = "New Chat",
    thread_id: str = "codex-thread-1",
) -> ActSession:
    session = ActSession(
        agent_id=agent_id,
        origin=origin,
        title=title,
        codex_thread_id=thread_id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_telegram_binding(
    db: Session,
    session: ActSession,
    api: FakeTelegramBotApi,
    *,
    topic_name: str,
    thread_id: int = 77,
    role: str = TELEGRAM_ACT_ROLE,
) -> tuple[FakeSecretStore, TelegramBotConnection, TelegramTopicSession]:
    store = FakeSecretStore()
    secret_reference = store.put("telegram-token", namespace="telegram")
    connection = TelegramBotConnection(
        role=role,
        is_default=True,
        secret_store_id=store.implementation_id,
        secret_reference=secret_reference,
        bot_id="1001",
        status="connected",
        paired_chat_id="123",
        paired_user_id="456",
        topics_enabled=True,
        allows_users_to_create_topics=True,
    )
    db.add(connection)
    db.flush()
    mapping = TelegramTopicSession(
        connection_id=connection.id,
        telegram_chat_id="123",
        message_thread_id=thread_id,
        session_id=session.id,
        topic_name=topic_name,
    )
    db.add(mapping)
    db.commit()
    api.topics[thread_id] = {"message_thread_id": thread_id, "name": topic_name}
    db.refresh(mapping)
    return store, connection, mapping


def _title_sync(
    db: Session,
    codex: _FakeCodexSessions,
    *,
    store: FakeSecretStore | None = None,
    api: FakeTelegramBotApi | None = None,
) -> TitleSyncService:
    return TitleSyncService(
        db,
        telegram_secret_store=store,
        telegram_api_factory=(lambda _token: api) if api is not None else FakeTelegramBotApi,
        codex_sessions_factory=lambda _agent_id: codex,
    )


def test_new_chat_stays_until_native_codex_name_arrives(db: Session) -> None:
    session_service = ActSessionService(
        db,
        agent_id="act",
        telegram_sync=lambda _db, _session: None,
    )
    session = session_service.create_session()
    session_service.enqueue_turn(session.id, "A message that must not become a title", commit=False)
    assert session.title == "New Chat"

    session.codex_thread_id = "codex-thread-1"
    db.commit()
    codex = _FakeCodexSessions("  Native   Codex title  ")
    assert _title_sync(db, codex).sync_from_codex(session, codex) is True

    db.refresh(session)
    assert session.title == "Native Codex title"


def test_dispatcher_syncs_title_only_after_successful_turn(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = _FakeRuntimeSessions("Native dispatcher title")
    app_server = _FakeAppServer(sessions)
    session = _make_session(db, thread_id="pending:dispatcher")
    turn = ActTurn(session_id=session.id, user_message="Complete this", status="running")
    db.add(turn)
    db.commit()

    monkeypatch.setattr("app.services.act_turn_dispatcher.act_app_server_service", app_server)
    monkeypatch.setattr(
        CodexRoutingService,
        "resolve",
        lambda _self, **_kwargs: SimpleNamespace(effective_model=None, effective_reasoning_effort=None),
    )
    monkeypatch.setattr(
        AgentPolicyService,
        "policy",
        lambda _self, _agent_id: SimpleNamespace(model=None, reasoning_effort=None),
    )

    ActTurnDispatcher()._execute(db, turn.id)

    db.refresh(session)
    db.refresh(turn)
    assert turn.status == "succeeded"
    assert session.title == "Native dispatcher title"
    assert sessions.reads == ["codex-thread-1"]


def test_subsequent_native_codex_name_updates_the_existing_topic(db: Session) -> None:
    api = FakeTelegramBotApi()
    session = _make_session(db, title="First title")
    store, _connection, mapping = _make_telegram_binding(db, session, api, topic_name="First title")
    codex = _FakeCodexSessions("First title")
    sync = _title_sync(db, codex, store=store, api=api)

    assert sync.sync_from_codex(session, codex) is False
    codex.name = "Second native title"
    assert sync.sync_from_codex(session, codex) is True

    db.refresh(session)
    db.refresh(mapping)
    assert session.title == "Second native title"
    assert mapping.topic_name == "Second native title"
    assert [call for call in api.calls if call[0] == "editForumTopic"] == [
        (
            "editForumTopic",
            {
                "chat_id": 123,
                "message_thread_id": 77,
                "name": "Second native title",
                "icon_custom_emoji_id": None,
            },
        )
    ]


def test_unchanged_native_title_is_a_no_op(db: Session) -> None:
    api = FakeTelegramBotApi()
    session = _make_session(db, title="Same title")
    store, _connection, mapping = _make_telegram_binding(db, session, api, topic_name="Same title")
    codex = _FakeCodexSessions("  Same   title ")
    sync = _title_sync(db, codex, store=store, api=api)
    before = session.updated_at

    assert sync.sync_from_codex(session, codex) is False
    assert sync.sync_from_codex(session, codex) is False

    db.refresh(session)
    db.refresh(mapping)
    assert session.updated_at == before
    assert mapping.topic_name == "Same title"
    assert not [call for call in api.calls if call[0] == "editForumTopic"]


def test_native_title_without_telegram_binding_updates_only_eidolon(db: Session) -> None:
    api = FakeTelegramBotApi()
    session = _make_session(db)
    codex = _FakeCodexSessions("Backend only title")

    assert _title_sync(db, codex, api=api).sync_from_codex(session, codex) is True

    db.refresh(session)
    assert session.title == "Backend only title"
    assert api.calls == []


@pytest.mark.parametrize("agent_id", ["act", "observer", "assistant"])
def test_native_title_sync_applies_to_act_observer_and_assistant(
    db: Session,
    agent_id: str,
) -> None:
    session = _make_session(db, agent_id=agent_id, title="New Chat")
    codex = _FakeCodexSessions(f"{agent_id} native title")

    assert _title_sync(db, codex).sync_from_codex(session, codex) is True

    db.refresh(session)
    assert session.title == f"{agent_id} native title"


def test_wecom_observer_session_uses_native_title_without_telegram(db: Session) -> None:
    session = _make_session(db, agent_id="observer", origin="wecom")
    codex = _FakeCodexSessions("WeCom observer title")

    assert _title_sync(db, codex).sync_from_codex(session, codex) is True

    db.refresh(session)
    assert session.title == "WeCom observer title"
    assert db.scalar(select(TelegramTopicSession).where(TelegramTopicSession.session_id == session.id)) is None


def test_telegram_manual_rename_updates_session_and_native_codex_name(db: Session) -> None:
    api = FakeTelegramBotApi()
    session = _make_session(db, title="Old title")
    store, connection, mapping = _make_telegram_binding(db, session, api, topic_name="Old title")
    codex = _FakeCodexSessions("Old title")
    title_sync = _title_sync(db, codex, store=store, api=api)
    telegram = TelegramService(
        db,
        secret_store=store,
        api_factory=lambda _token: api,
        role=TELEGRAM_ACT_ROLE,
        title_sync=title_sync,
    )
    event = {
        "chat_id": "123",
        "user_id": "456",
        "message_thread_id": 77,
        "forum_topic_edited": {"name": "  Telegram   rename  "},
    }

    telegram._handle_message(connection.id, connection.bot_id, event)
    telegram._handle_message(connection.id, connection.bot_id, event)
    assert title_sync.sync_from_codex(session, codex) is False

    db.refresh(session)
    db.refresh(mapping)
    assert session.title == "Telegram rename"
    assert mapping.topic_name == "Telegram rename"
    assert codex.sets == [("codex-thread-1", "Telegram rename")]
    assert not [call for call in api.calls if call[0] == "editForumTopic"]


def test_telegram_projection_failure_keeps_native_backend_title(db: Session) -> None:
    api = FakeTelegramBotApi()
    session = _make_session(db, title="Old topic title")
    store, _connection, mapping = _make_telegram_binding(db, session, api, topic_name="Old topic title")
    codex = _FakeCodexSessions("Canonical native title")
    api.errors.append(TelegramProviderError("transport", "Telegram is unavailable"))

    assert _title_sync(db, codex, store=store, api=api).sync_from_codex(session, codex) is True

    db.refresh(session)
    db.refresh(mapping)
    assert session.title == "Canonical native title"
    assert mapping.topic_name == "Old topic title"
    assert not [call for call in api.calls if call[0] == "editForumTopic"]


def test_native_app_server_thread_name_methods_use_thread_contracts() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict, int]] = []

        def request(self, method: str, params: dict, *, timeout: int | None = None):
            self.calls.append((method, params, timeout or 0))
            if method == "thread/read":
                return {"thread": {"name": "Native name"}}
            return {}

    client = _Client()
    service = ProductManagerSessionService(client)  # type: ignore[arg-type]

    assert service.read_thread_name("thread-1") == "Native name"
    service.set_thread_name("thread-1", "Renamed natively")

    assert client.calls == [
        ("thread/read", {"threadId": "thread-1"}, 10),
        ("thread/name/set", {"threadId": "thread-1", "name": "Renamed natively"}, 10),
    ]
