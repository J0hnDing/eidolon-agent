from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.execution.context import InvocationContext
from app.execution.handlers.agent_private import AgentPrivateHandler
from app.execution.types import InvocationExecutionError, InvocationTargetRef
from app.models import (
    ActLiveMessage,
    ActSession,
    ActTurn,
    TelegramBotConnection,
    TelegramTopicSession,
)
from app.schemas.act import ActSessionRead
from app.schemas.agents import AgentPolicyUpdate
from app.services.act_chat_message_service import (
    ActChatMessageCapability,
    ActChatMessageError,
    accept_act_reply,
)
from app.services.act_session_service import ActSessionError, ActSessionService
from app.services.agent_dispatchers import agent_dispatchers
from app.services.agent_policy_service import AgentPolicyService
from app.services.function_catalog_service import FunctionCatalogService
from app.services.mcp_function_service import McpFunctionError, McpFunctionService
from app.services.secret_store import FakeSecretStore
from app.services.telegram_provider import FakeTelegramBotApi
from app.services.telegram_service import (
    TELEGRAM_ACT_ROLE,
    TELEGRAM_ASSISTANT_ROLE,
    TelegramService,
    TelegramServiceError,
)


@pytest.fixture
def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'live-chat.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _running(db: Session, *, agent_id: str = "act") -> tuple[ActSession, ActTurn]:
    session = ActSession(agent_id=agent_id, codex_thread_id=f"thread-{uuid4()}")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Log in to MarkUs", status="running")
    db.add(turn)
    db.commit()
    return session, turn


def _prompt(db: Session, turn: ActTurn, *, expires_in: int = 120) -> ActLiveMessage:
    prompt = ActLiveMessage(
        turn_id=turn.id, role="assistant", text="Enter 123 in Duo Mobile and reply done",
        reply_status="awaiting", reply_expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )
    db.add(prompt)
    db.commit()
    return prompt


def _telegram(db: Session, session: ActSession, turn: ActTurn):
    if session.agent_id == "assistant":
        from app.services.assistant_service import AssistantService

        AssistantService(db).state().primary_session_id = session.id
    role = TELEGRAM_ASSISTANT_ROLE if session.agent_id == "assistant" else TELEGRAM_ACT_ROLE
    store = FakeSecretStore()
    reference = store.put("123:telegram-token", namespace="telegram")
    connection = TelegramBotConnection(
        role=role, is_default=True, secret_store_id=store.implementation_id,
        secret_reference=reference, bot_id="1001", status="connected", paired_chat_id="42",
        paired_user_id="7", topics_enabled=True, allows_users_to_create_topics=True,
    )
    db.add(connection)
    db.flush()
    mapping = TelegramTopicSession(
        connection_id=connection.id, telegram_chat_id="42", message_thread_id=91,
        session_id=session.id, topic_name="MarkUs",
    )
    if session.agent_id != "assistant":
        db.add(mapping)
    turn.delivery_provider = "telegram"
    turn.delivery_connection_id = connection.id
    turn.delivery_chat_id = "42"
    turn.delivery_message_thread_id = 91 if session.agent_id != "assistant" else None
    db.commit()
    api = FakeTelegramBotApi()
    service = TelegramService(
        db, role=role, secret_store=store, api_factory=lambda _token: api,
    )
    return service, api, connection, mapping


def test_web_message_is_visible_immediately_while_turn_remains_running(db: Session):
    session, turn = _running(db)
    result = ActChatMessageCapability(db).send(session.id, "Duo code: 123")

    assert result == {
        "status": "sent", "message_id": result["message_id"], "telegram_message_id": None,
        "reply_status": "not_requested", "reply": None,
    }
    # Read from an independent API session, as the polling Chat surface does.
    with Session(db.get_bind()) as api_db:
        data = ActSessionRead.model_validate(ActSessionService(api_db).read_session(session.id))
        assert len(data.turns) == 1
        assert data.turns[0].id == turn.id
        assert data.turns[0].status == "running"
        assert data.turns[0].assistant_message is None
        assert [(message.role, message.text) for message in data.turns[0].live_messages] == [
            ("assistant", "Duo code: 123"),
        ]


def test_wait_consumes_reply_in_same_turn_without_dispatching_or_restarting(
    db: Session, monkeypatch: pytest.MonkeyPatch,
):
    session, turn = _running(db)
    session_id, turn_id = session.id, turn.id
    bind = db.get_bind()
    notifications = []
    monkeypatch.setattr(agent_dispatchers["act"], "notify", lambda: notifications.append(True))

    def send_and_wait():
        with Session(bind) as tool_db:
            return ActChatMessageCapability(tool_db).send(session_id, "Duo code: 123", True, 5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(send_and_wait)
        deadline = monotonic() + 3
        while True:
            db.rollback()
            prompt = db.scalar(select(ActLiveMessage).where(ActLiveMessage.turn_id == turn_id))
            if prompt is not None:
                break
            assert monotonic() < deadline, "Tool did not publish the prompt before waiting"
            sleep(0.02)
        assert prompt.reply_status == "awaiting"
        assert not pending.done()
        returned_turn = ActSessionService(db).enqueue_turn(session_id, "done")
        assert returned_turn.id == turn_id
        assert returned_turn.status == "running"
        result = pending.result(timeout=3)
        assert result["reply"] == "done"
        assert result["reply_status"] == "received"
    assert notifications == []
    assert db.scalar(select(func.count()).select_from(ActTurn)) == 1
    assert [(message.role, message.text) for message in returned_turn.live_messages] == [
        ("assistant", "Duo code: 123"), ("user", "done"),
    ]
    with pytest.raises(ActSessionError, match="already queued or running"):
        ActSessionService(db).enqueue_turn(session_id, "another reply")
    assert db.scalar(select(func.count()).select_from(ActLiveMessage).where(ActLiveMessage.role == "user")) == 1


def test_reply_claim_is_atomic_and_isolated_to_current_session(db: Session):
    _session, turn = _running(db)
    other_session, other_turn = _running(db)
    prompt = _prompt(db, turn)
    turn_id, prompt_id = turn.id, prompt.id
    bind = db.get_bind()
    with pytest.raises(ActSessionError, match="already queued or running"):
        ActSessionService(db).enqueue_turn(other_session.id, "wrong conversation")
    db.rollback()

    def claim(text):
        with Session(bind) as reply_db:
            claimed = accept_act_reply(reply_db, reply_db.get(ActTurn, turn_id), text)
            reply_db.commit()
            return claimed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["done", "approved"]))
    assert sorted(results) == [False, True]
    db.expire_all()
    replies = list(db.scalars(select(ActLiveMessage).where(ActLiveMessage.role == "user")))
    assert len(replies) == 1
    assert replies[0].reply_to_message_id == prompt_id
    assert replies[0].turn_id == turn_id
    assert db.get(ActLiveMessage, prompt_id).reply_status == "answered"
    assert db.scalar(select(ActLiveMessage).where(ActLiveMessage.turn_id == other_turn.id)) is None


def test_send_wait_times_out_without_treating_it_as_approval(db: Session):
    session, turn = _running(db)
    result = ActChatMessageCapability(db).send(session.id, "Enter 123 in Duo Mobile", True, 1)
    assert result["status"] == "sent"
    assert result["reply_status"] == "timed_out"
    assert result["reply"] is None
    assert db.get(ActLiveMessage, result["message_id"]).reply_status == "expired"
    assert turn.status == "running"
    with pytest.raises(ActSessionError, match="already queued or running"):
        ActSessionService(db).enqueue_turn(session.id, "too late")


@pytest.mark.parametrize("condition,expected", [("expired", "timed_out"), ("cancelled", "cancelled"), ("completed", "cancelled")])
def test_wait_releases_on_deadline_or_turn_cancellation(db: Session, condition, expected):
    session, turn = _running(db)
    prompt = _prompt(db, turn, expires_in=-1 if condition == "expired" else 120)
    if condition == "cancelled":
        turn.cancel_requested_at = datetime.now(UTC)
    elif condition == "completed":
        turn.status = "completed"
    db.commit()
    assert not accept_act_reply(db, turn, "late approval")
    assert ActChatMessageCapability(db)._wait_for_reply(prompt.id, turn.id) == (expected, None)
    db.refresh(prompt)
    assert prompt.reply_status == ("expired" if condition == "expired" else "cancelled")
    if condition != "completed":
        with pytest.raises(ActSessionError, match="already queued or running"):
            ActSessionService(db).enqueue_turn(session.id, "late approval")


@pytest.mark.parametrize("arguments", [
    {"text": ""}, {"text": "   "}, {"text": "x" * 4001}, {"text": None},
    {"text": "hello", "wait_for_reply": "yes"},
    {"text": "hello", "timeout_seconds": True}, {"text": "hello", "timeout_seconds": 0},
    {"text": "hello", "timeout_seconds": 121}, {"text": "hello", "timeout_seconds": 1.5},
])
def test_invalid_messages_never_publish(db: Session, arguments):
    session, _turn = _running(db)
    with pytest.raises(ActChatMessageError) as error:
        ActChatMessageCapability(db).send(session.id, **arguments)
    assert error.value.error_type == "invalid_input"
    assert db.scalar(select(ActLiveMessage)) is None


@pytest.mark.parametrize("condition", ["observer", "archived", "queued", "cancelled", "wecom", "missing"])
def test_send_requires_supported_running_act_context(db: Session, condition):
    session, turn = _running(db, agent_id="observer" if condition == "observer" else "act")
    if condition == "archived":
        session.status = "archived"
    elif condition == "queued":
        turn.status = "queued"
    elif condition == "cancelled":
        turn.cancel_requested_at = datetime.now(UTC)
    elif condition == "wecom":
        turn.delivery_provider = "wecom"
    db.commit()
    with pytest.raises(ActChatMessageError) as error:
        ActChatMessageCapability(db).send(-1 if condition == "missing" else session.id, "hello")
    assert error.value.error_type == ("unsupported_transport" if condition == "wecom" else "act_turn_unavailable")
    assert db.scalar(select(ActLiveMessage)) is None


def test_private_mcp_discovery_invocation_and_live_policy_revocation(db: Session, monkeypatch):
    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda _self: [])
    name = McpFunctionService.tool_name("agent_private", "chat.send_message")
    assert name not in {tool.name for tool in McpFunctionService(db).list_tools()}
    for agent_id in ("observer",):
        session, _turn = _running(db, agent_id=agent_id)
        token = AgentPolicyService(db).issue(agent_id, session.id)
        client = McpFunctionService(db, agent_token=token)
        assert name not in {tool.name for tool in client.list_tools()}
        with pytest.raises(McpFunctionError) as error:
            client.invoke(name, {"text": "hello"})
        assert error.value.error_type == "unknown_tool"
    session, turn = _running(db)
    token = AgentPolicyService(db).issue("act", session.id)
    client = McpFunctionService(db, agent_token=token)
    assert name in {tool.name for tool in client.list_tools()}
    result = client.invoke(name, {"text": "Duo code: 123"}).output
    assert result["status"] == "sent"
    assert db.get(ActLiveMessage, result["message_id"]).turn_id == turn.id
    with pytest.raises(McpFunctionError) as error:
        client.invoke(name, {"text": "hello", "session_id": 999})
    assert error.value.error_type == "invalid_input"
    AgentPolicyService(db).update("act", AgentPolicyUpdate(banned_functions=["chat.send_message"]))
    assert name not in {tool.name for tool in client.list_tools()}
    with pytest.raises(McpFunctionError) as error:
        client.invoke(name, {"text": "banned message"})
    assert error.value.error_type == "agent_permission_denied"


@pytest.mark.parametrize("principal,agent", [("user", None), ("skill", None), ("agent", "observer"), ("agent", "assistant")])
def test_direct_handler_rejects_non_act_principals(db: Session, principal, agent):
    session, _turn = _running(db)
    context = InvocationContext(principal_kind=principal, origin="backend", agent_id=agent, agent_session_id=session.id)
    with pytest.raises(InvocationExecutionError) as error:
        AgentPrivateHandler(db).execute(InvocationTargetRef("agent_private", "chat.send_message"), {"text": "hello"}, context)
    assert error.value.error_type == "agent_permission_denied"
    assert db.scalar(select(ActLiveMessage)) is None


@pytest.mark.parametrize("field", ["session_id", "chat_id", "message_thread_id", "connection_id"])
def test_direct_handler_rejects_arbitrary_destinations(db: Session, field):
    session, _turn = _running(db)
    context = InvocationContext(principal_kind="agent", origin="agent_mcp", agent_id="act", agent_session_id=session.id)
    with pytest.raises(InvocationExecutionError) as error:
        AgentPrivateHandler(db).execute(InvocationTargetRef("agent_private", "chat.send_message"), {"text": "hello", field: 999}, context)
    assert error.value.error_type == "invalid_input"
    assert db.scalar(select(ActLiveMessage)) is None


def test_handler_rechecks_session_ownership_and_explicit_ban(db: Session):
    session, _turn = _running(db, agent_id="assistant")
    context = InvocationContext(principal_kind="agent", origin="agent_mcp", agent_id="act", agent_session_id=session.id)
    handler = AgentPrivateHandler(db)
    target = InvocationTargetRef("agent_private", "chat.send_message")
    with pytest.raises(InvocationExecutionError) as error:
        handler.execute(target, {"text": "hello"}, context)
    assert error.value.error_type == "agent_permission_denied"
    session.agent_id = "act"
    db.commit()
    AgentPolicyService(db).update("act", AgentPolicyUpdate(banned_functions=["chat.send_message"]))
    with pytest.raises(InvocationExecutionError) as error:
        handler.execute(target, {"text": "hello"}, context)
    assert error.value.error_type == "agent_permission_denied"
    assert db.scalar(select(ActLiveMessage)) is None


@pytest.mark.parametrize("agent_id", ["act", "assistant"])
def test_telegram_sends_immediately_to_original_topic_and_accepts_reply(db: Session, agent_id):
    session, turn = _running(db, agent_id=agent_id)
    telegram, api, connection, _mapping = _telegram(db, session, turn)
    result = ActChatMessageCapability(db, telegram_service=telegram).send(session.id, "Duo code: 123")
    assert result["telegram_message_id"] == 1
    assert api.sent_messages[0]["chat"] == {"id": 42}
    assert api.sent_messages[0]["message_thread_id"] == (91 if agent_id == "act" else None)
    assert api.sent_messages[0]["text"] == "Duo code: 123"
    assert turn.status == "running"
    _prompt(db, turn)
    telegram._handle_message(connection.id, connection.bot_id, {
        "chat_id": 42, "user_id": 7, "message_thread_id": 91 if agent_id == "act" else None, "text": "done",
    })
    assert len(api.sent_messages) == 1  # No busy error or new thinking/final message.
    assert db.scalar(select(func.count()).select_from(ActTurn)) == 1
    assert db.scalar(select(ActLiveMessage.text).where(ActLiveMessage.role == "user")) == "done"


def test_telegram_reply_requires_paired_identity_and_current_topic(db: Session):
    session, turn = _running(db)
    telegram, api, connection, _mapping = _telegram(db, session, turn)
    prompt = _prompt(db, turn)
    for fields in ({"chat_id": 99}, {"user_id": 8}):
        telegram._handle_message(connection.id, connection.bot_id, {
            "chat_id": 42, "user_id": 7, "message_thread_id": 91, "text": "done", **fields,
        })
    db.refresh(prompt)
    assert prompt.reply_status == "awaiting"
    assert db.scalar(select(ActLiveMessage).where(ActLiveMessage.role == "user")) is None
    assert api.sent_messages == []


@pytest.mark.parametrize("agent_id", ["act", "assistant"])
def test_telegram_live_message_renders_markdown_without_changing_saved_text(db: Session, agent_id):
    session, turn = _running(db, agent_id=agent_id)
    telegram, api, _connection, _mapping = _telegram(db, session, turn)
    text = 'I saved **your plan**. [Open it](https://example.com/plan) <script>alert(1)</script>'

    result = ActChatMessageCapability(db, telegram_service=telegram).send(session.id, text)

    delivered = api.sent_messages[-1]
    assert delivered["parse_mode"] == "HTML"
    assert "**" not in delivered["text"]
    assert '<a href="https://example.com/plan">Open it</a>' in delivered["text"]
    assert "<script>" not in delivered["text"]
    assert "&lt;script&gt;" in delivered["text"]
    assert delivered["message_thread_id"] == (91 if agent_id == "act" else None)
    assert db.get(ActLiveMessage, result["message_id"]).text == text
    assert turn.status == "running"


@pytest.mark.parametrize("agent_id", ["act", "assistant"])
@pytest.mark.parametrize("plain_draft", [False, True])
def test_telegram_final_reply_and_draft_render_markdown(db: Session, agent_id, plain_draft):
    session, turn = _running(db, agent_id=agent_id)
    telegram, api, connection, _mapping = _telegram(db, session, turn)
    if plain_draft:
        api.send_rich_message_draft = None
    text = 'I saved **your plan**. [Open it](https://example.com/plan)'

    telegram.send_agent_turn_result(
        connection, text, message_thread_id=turn.delivery_message_thread_id, draft_id=turn.id,
    )

    delivered = api.sent_messages[-1]
    assert delivered["parse_mode"] == "HTML"
    assert "**" not in delivered["text"]
    assert '<a href="https://example.com/plan">Open it</a>' in delivered["text"]
    assert delivered["message_thread_id"] == (91 if agent_id == "act" else None)
    if plain_draft:
        draft = next(payload for name, payload in api.calls if name == "sendMessageDraft")
        assert "**" not in draft["text"]
        assert "[Open it]" not in draft["text"]
        assert "https://example.com/plan" in draft["text"]
    else:
        draft = next(payload for name, payload in api.calls if name == "sendRichMessageDraft")
        assert draft["rich_message"]["html"] == delivered["text"]


def test_telegram_live_message_splits_unicode_within_limits_and_keeps_destination(db: Session):
    session, turn = _running(db, agent_id="assistant")
    telegram, api, _connection, _mapping = _telegram(db, session, turn)
    text = "🌞" * 2500

    result = ActChatMessageCapability(db, telegram_service=telegram).send(session.id, text)

    assert len(api.sent_messages) == 2
    assert "".join(message["text"] for message in api.sent_messages) == text
    assert all(message["chat"]["id"] == 42 and message["message_thread_id"] is None
               and message["parse_mode"] == "HTML" for message in api.sent_messages)
    assert result["telegram_message_id"] == api.sent_messages[-1]["message_id"]
    assert db.get(ActLiveMessage, result["message_id"]).text == text


def test_assistant_live_message_rejects_changed_chat_with_assistant_error(db: Session):
    session, turn = _running(db, agent_id="assistant")
    telegram, api, connection, _mapping = _telegram(db, session, turn)
    connection.paired_chat_id = "99"
    db.commit()
    with pytest.raises(TelegramServiceError, match="Assistant turn's paired Telegram chat") as error:
        ActChatMessageCapability(db, telegram_service=telegram).send(session.id, "Hello")
    assert error.value.error_type == "telegram_chat_unavailable"
    assert api.sent_messages == []


@pytest.mark.parametrize("mismatch", ["connection", "chat", "topic", "missing_mapping"])
def test_telegram_destination_drift_rejects_without_sending_and_fails_prompt(db: Session, mismatch):
    session, turn = _running(db)
    telegram, api, _connection, mapping = _telegram(db, session, turn)
    if mismatch == "connection":
        turn.delivery_connection_id = 999
    elif mismatch == "chat":
        turn.delivery_chat_id = "99"
    elif mismatch == "topic":
        turn.delivery_message_thread_id = 92
    else:
        db.delete(mapping)
    db.commit()
    with pytest.raises(TelegramServiceError) as error:
        ActChatMessageCapability(db, telegram_service=telegram).send(session.id, "Duo code: 123", True)
    assert error.value.error_type == "telegram_topic_unavailable"
    assert api.sent_messages == []
    prompt = db.scalar(select(ActLiveMessage))
    assert prompt.reply_status == "failed"
    assert not accept_act_reply(db, turn, "done")


def test_overlapping_reply_wait_is_refused_before_remote_send(db: Session):
    session, turn = _running(db)
    telegram, api, _connection, _mapping = _telegram(db, session, turn)
    original = _prompt(db, turn)
    original_id = original.id
    with pytest.raises(ActChatMessageError) as error:
        ActChatMessageCapability(db, telegram_service=telegram).send(session.id, "Second prompt", True)
    assert error.value.error_type == "reply_wait_active"
    assert api.sent_messages == []
    prompts = list(db.scalars(select(ActLiveMessage)))
    assert len(prompts) == 1
    assert prompts[0].id == original_id
    assert prompts[0].reply_status == "awaiting"
