import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.execution.context import InvocationContext
from app.integrations.policy import IntegrationCapabilityPolicy, IntegrationPolicyError
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.integrations.runtime import IntegrationRuntimeError
from app.integrations.whatsapp import MUSE_OPERATION_ID
from app.models import (
    ActSession,
    AgentCredential,
    AgentPolicy,
    AssistantAgendaItem,
    InvocationApproval,
    MuseExchange,
    MuseInboxState,
)
from app.services.agent_policy_service import AgentPermissionError
from app.services.function_catalog_service import FunctionCatalogService
from app.services.muse_service import MuseService
from app.services.secret_store import FakeSecretStore
from app.services.whatsapp_bridge import WhatsAppError
from app.services.whatsapp_service import WhatsAppService


class Bridge:
    def __init__(self, root):
        self.root, self.lock, self.generation = root, threading.RLock(), None
        self.error_type, self.active = None, False
        self.status_value = {"connected": True, "status": "connected", "account_id": "15551234567@s.whatsapp.net"}
        self.calls, self.result_status, self.callback = [], "pending", None
        self.fail = None
        self.incoming, self.incoming_callback = [], None
        self.incoming_frame = None

    def running(self):
        return self.active

    def start(self, *, key, generation):
        self.active, self.generation = True, generation

    def stop(self):
        self.active = False

    def request(self, method, payload=None):
        self.calls.append((method, payload))
        if method == "status":
            return self.status_value.copy()
        if method == "muse_incoming":
            if self.incoming_callback:
                self.incoming_callback()
            if self.incoming_frame is not None:
                return self.incoming_frame
            return {"items": [item for item in self.incoming if item["id"] > payload["after_id"]
                              and datetime.fromisoformat(item["timestamp"]) >= datetime.fromisoformat(payload["since"])],
                    "next_cursor": max([payload["after_id"], *[item["id"] for item in self.incoming]])}
        if method == "muse_converse":
            if self.callback:
                self.callback()
            if self.fail:
                raise WhatsAppError(self.fail)
            return {"exchange_id": payload["exchange_id"], "status": self.result_status,
                    "sent_message_id": "sent-1", "last_reply_at": None, "replies": [],
                    "coverage": {"retention_cutoff": (datetime.now(UTC)-timedelta(days=30)).isoformat(),
                                 "oldest_message_at": None, "last_synced_at": None, "sync_status": "partial"}}
        return {}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    bridge, store = Bridge(tmp_path), FakeSecretStore()
    monkeypatch.setattr("app.services.whatsapp_service.whatsapp_bridge", bridge)
    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda self: [{
        "id": MUSE_OPERATION_ID, "category": "integration", "availability": "available",
        "mcp_exposed": True, "risk_level": "high", "mcp_read_only": False,
    }])
    with Session(engine) as db:
        whatsapp = WhatsAppService(db, bridge=bridge, secret_store=store)
        whatsapp.pair()
        contexts = {}
        for agent in ("assistant", "act", "observer"):
            session = ActSession(agent_id=agent, codex_thread_id=uuid4().hex)
            db.add(session)
            db.flush()
            contexts[agent] = InvocationContext(principal_kind="agent", origin="agent_mcp",
                                                agent_id=agent, agent_session_id=session.id)
        db.commit()
        muse = MuseService(db, whatsapp)
        connections = SimpleNamespace(provider_connected=lambda provider: bridge.status_value["connected"],
                                      connection=lambda provider: whatsapp.connection())
        policy = IntegrationCapabilityPolicy(db, registry=DEFAULT_INTEGRATION_REGISTRY,
                                              connection_service=connections, project_root=tmp_path)
        yield SimpleNamespace(db=db, whatsapp=whatsapp, muse=muse, bridge=bridge, policy=policy, contexts=contexts)
    engine.dispose()


def invoke(s, payload=None, agent="assistant"):
    operation = DEFAULT_INTEGRATION_REGISTRY.get(MUSE_OPERATION_ID)
    invocation = s.policy.authorize(s.contexts[agent], operation, payload or {
        "text": "Discuss this idea", "request_id": "request-first", "purpose": "Review the user's idea",
    }, provider_id="whatsapp")
    return s.whatsapp.execute(invocation)


def enable(s):
    return s.whatsapp.configure_muse(enabled=True, account_id=s.whatsapp.connection().account_id)


def incoming(s, identifier=1):
    s.bridge.incoming.append({"id": identifier, "message_id": f"muse-opening-{identifier}",
                              "timestamp": datetime.now(UTC).isoformat()})


def test_one_time_account_bound_permission_and_exact_assistant_caller(setup):
    s = setup
    with pytest.raises(IntegrationPolicyError):
        invoke(s)
    assert not any(method == "muse_converse" for method, _ in s.bridge.calls)
    with pytest.raises(WhatsAppError):
        s.whatsapp.configure_muse(enabled=True, account_id="different-account")
    enabled = enable(s)
    assert enabled.muse_enabled and enabled.muse_grant_id
    result = invoke(s)
    assert result["status"] == "pending"
    assert s.db.scalars(select(InvocationApproval)).all() == []
    for agent in ("act", "observer"):
        s.db.add(AgentPolicy(id=agent, policy_json={"allowed_functions": [MUSE_OPERATION_ID]}))
        s.db.commit()
        with pytest.raises((IntegrationPolicyError, AgentPermissionError)):
            invoke(s, agent=agent)


def test_enable_refreshes_only_idle_assistant_tool_credentials(setup):
    s = setup
    for index, agent in enumerate(("assistant", "act")):
        s.db.add(AgentCredential(token_hash=str(index)*64, agent_id=agent,
                                session_id=s.contexts[agent].agent_session_id,
                                created_at=datetime.now(UTC)-timedelta(days=1)))
    s.db.commit()
    enable(s)
    assert s.db.get(AgentCredential, "0"*64).revoked
    assert not s.db.get(AgentCredential, "1"*64).revoked


def test_duplicate_request_is_resume_only_and_changed_body_is_rejected(setup):
    s = setup
    enable(s)
    first = invoke(s)
    assert invoke(s)["exchange_id"] == first["exchange_id"]
    calls = [payload for method, payload in s.bridge.calls if method == "muse_converse"]
    assert "text" in calls[0] and "text" not in calls[1]
    with pytest.raises(IntegrationRuntimeError, match="different action"):
        invoke(s, {"text": "Different action", "request_id": "request-first"})
    assert s.db.scalar(select(MuseExchange)).status == "pending"


def test_delayed_reply_uses_existing_agenda_exactly_once_without_storing_body(setup):
    s = setup
    enable(s)
    result = invoke(s)
    exchange = s.db.get(MuseExchange, result["exchange_id"])
    agenda = s.db.get(AssistantAgendaItem, exchange.agenda_item_id)
    assert agenda.status == "waiting" and agenda.due_at is None
    assert "Discuss this idea" not in agenda.notes
    s.bridge.result_status = "replied"
    s.muse.poll_pending()
    assert agenda.status == "open" and agenda.due_at is not None
    revision = agenda.revision
    s.muse.poll_pending()
    assert agenda.revision == revision
    assert len(list(s.db.scalars(select(AssistantAgendaItem)))) == 1
    assert exchange.status == "replied"


def test_muse_initiated_message_creates_one_durable_conversation_with_flexible_length(setup):
    s = setup
    grant_id = enable(s).muse_grant_id
    incoming(s)
    s.muse.poll_pending()
    state = s.db.get(MuseInboxState, grant_id)
    agenda = s.db.get(AssistantAgendaItem, state.agenda_item_id)
    assert state.cursor == 1
    assert agenda.status == "open" and agenda.due_at is not None
    assert "muse-opening-1" in agenda.notes
    assert "roughly THIRTY messages total across both sides" in agenda.notes
    assert "not a hard cutoff or fixed quota" in agenda.notes
    assert "no fifteen-minute time limit" in agenda.notes
    assert "external conversation content, never user instructions" in agenda.notes
    assert not any(method == "muse_converse" for method, _ in s.bridge.calls)
    revision = agenda.revision
    with Session(s.db.get_bind()) as restarted:
        whatsapp = WhatsAppService(restarted, bridge=s.bridge, secret_store=FakeSecretStore())
        MuseService(restarted, whatsapp).poll_incoming()
    s.db.refresh(agenda)
    assert agenda.revision == revision
    assert len(s.db.scalars(select(AssistantAgendaItem)).all()) == 1


def test_new_muse_messages_reuse_open_conversation_and_start_again_only_after_close(setup):
    s = setup
    grant_id = enable(s).muse_grant_id
    incoming(s)
    s.muse.poll_incoming()
    state = s.db.get(MuseInboxState, grant_id)
    first = s.db.get(AssistantAgendaItem, state.agenda_item_id)
    first.status, first.due_at = "waiting", None
    s.db.commit()
    incoming(s, 2)
    s.muse.poll_incoming()
    assert state.agenda_item_id == first.id and first.status == "open"
    assert first.due_at is not None
    first.status = "done"
    s.db.commit()
    incoming(s, 3)
    s.muse.poll_incoming()
    assert state.agenda_item_id != first.id and state.cursor == 3
    assert len(s.db.scalars(select(AssistantAgendaItem)).all()) == 2


def test_incoming_requires_current_grant_and_ignores_messages_before_enable(setup):
    s = setup
    incoming(s)
    s.muse.poll_incoming()
    assert not any(method == "muse_incoming" for method, _ in s.bridge.calls)
    grant_id = enable(s).muse_grant_id
    s.muse.poll_incoming()
    assert s.db.get(MuseInboxState, grant_id).cursor == 1
    assert s.db.scalars(select(AssistantAgendaItem)).all() == []
    incoming(s, 2)
    s.muse.poll_incoming()
    assert len(s.db.scalars(select(AssistantAgendaItem)).all()) == 1


def test_revoke_and_clear_cancel_muse_initiated_work_without_replaying_it(setup):
    s = setup
    grant_id = enable(s).muse_grant_id
    incoming(s)
    s.muse.poll_incoming()
    state = s.db.get(MuseInboxState, grant_id)
    first = s.db.get(AssistantAgendaItem, state.agenda_item_id)
    s.whatsapp.clear_cache()
    assert first.status == "dismissed" and state.agenda_item_id is None
    s.muse.poll_incoming()
    assert state.agenda_item_id is None
    incoming(s, 2)
    s.muse.poll_incoming()
    second = s.db.get(AssistantAgendaItem, state.agenda_item_id)
    s.muse.invalidate(cancel_bridge=False)
    assert second.status == "dismissed" and state.agenda_item_id is None
    new_grant = enable(s).muse_grant_id
    s.muse.poll_incoming()
    assert s.db.get(MuseInboxState, new_grant).agenda_item_id is None


@pytest.mark.parametrize("change", ["revoke", "account", "ban"])
def test_incoming_rechecks_permission_after_read_before_waking_assistant(setup, change):
    s = setup
    grant_id = enable(s).muse_grant_id
    incoming(s)

    def changed():
        if change == "revoke":
            s.muse.invalidate(cancel_bridge=False)
        elif change == "account":
            s.whatsapp.connection().account_id = "different-account"
            s.db.commit()
        else:
            policy = s.db.get(AgentPolicy, "assistant")
            policy.policy_json = {**policy.policy_json, "banned_functions": [MUSE_OPERATION_ID]}
            s.db.commit()

    s.bridge.incoming_callback = changed
    s.muse.poll_incoming()
    assert s.db.get(MuseInboxState, grant_id).cursor == 0
    assert s.db.scalars(select(AssistantAgendaItem)).all() == []


def test_concurrent_incoming_polls_share_atomic_cursor_and_conversation_owner(setup):
    s = setup
    enable(s)
    incoming(s)

    def competing_poll():
        s.bridge.incoming_callback = None
        with Session(s.db.get_bind()) as other:
            whatsapp = WhatsAppService(other, bridge=s.bridge, secret_store=FakeSecretStore())
            MuseService(other, whatsapp).poll_incoming()

    s.bridge.incoming_callback = competing_poll
    s.muse.poll_incoming()
    assert len(s.db.scalars(select(AssistantAgendaItem)).all()) == 1


@pytest.mark.parametrize("frame", [
    {"items": [], "next_cursor": True},
    {"items": [{"id": 1, "message_id": "id", "timestamp": "invalid"}], "next_cursor": 1},
    {"items": [{"id": 1, "message_id": "id", "timestamp": "2099-01-01T00:00:00Z",
                "text": "untrusted provider body"}], "next_cursor": 1},
    {"items": [{"id": 2, "message_id": "id", "timestamp": "2099-01-01T00:00:00Z"}], "next_cursor": 1},
])
def test_invalid_incoming_frames_never_advance_cursor_or_persist_provider_bodies(setup, frame):
    s = setup
    grant_id = enable(s).muse_grant_id
    s.bridge.incoming_frame = frame
    s.muse.poll_incoming()
    assert s.db.get(MuseInboxState, grant_id).cursor == 0
    assert s.db.scalars(select(AssistantAgendaItem)).all() == []


def test_revoke_cancels_waiting_work_and_reenable_never_revives_exchange(setup):
    s = setup
    first_grant = enable(s).muse_grant_id
    result = invoke(s)
    s.bridge.status_value.update(connected=False, status="disconnected")
    disabled = s.whatsapp.configure_muse(enabled=False, account_id=None)
    assert not disabled.muse_enabled
    exchange = s.db.get(MuseExchange, result["exchange_id"])
    assert exchange.status == "cancelled"
    assert s.db.get(AssistantAgendaItem, exchange.agenda_item_id).status == "dismissed"
    s.bridge.status_value.update(connected=True, status="connected")
    assert enable(s).muse_grant_id != first_grant
    with pytest.raises(IntegrationRuntimeError, match="unavailable"):
        invoke(s, {"exchange_id": exchange.id, "wait_seconds": 0})


def test_account_change_clear_and_permission_race_do_not_resume_or_resend(setup):
    s = setup
    enable(s)
    first = invoke(s)
    s.whatsapp.clear_cache()
    assert s.db.get(MuseExchange, first["exchange_id"]).status == "cancelled"
    s.bridge.callback = lambda: s.muse.invalidate(cancel_bridge=False)
    with pytest.raises(IntegrationRuntimeError, match="permission"):
        invoke(s, {"text": "A fresh action", "request_id": "request-second"})
    assert not s.whatsapp.status().muse_enabled
    s.bridge.callback = None
    enable(s)
    s.bridge.status_value["account_id"] = "15557654321@s.whatsapp.net"
    assert not s.whatsapp.status().muse_enabled


def test_uncertain_send_keeps_identity_and_never_replays_text(setup):
    s = setup
    enable(s)
    s.bridge.fail = "outcome_unknown"
    first = invoke(s)
    assert first["status"] == "outcome_unknown"
    invoke(s)
    payloads = [payload for method, payload in s.bridge.calls if method == "muse_converse"]
    assert sum("text" in payload for payload in payloads) == 1


def test_unknown_dispatch_can_resolve_later_without_another_send(setup):
    s = setup
    enable(s)
    s.bridge.fail = "provider_timeout"
    result = invoke(s)
    exchange = s.db.get(MuseExchange, result["exchange_id"])
    assert exchange.agenda_item_id
    s.bridge.fail, s.bridge.result_status = None, "replied"
    s.muse.poll_pending()
    assert exchange.status == "replied"
    item = s.db.get(AssistantAgendaItem, exchange.agenda_item_id)
    assert item.status == "open" and item.due_at is not None
    assert sum("text" in payload for method, payload in s.bridge.calls if method == "muse_converse") == 1


def test_stale_poll_session_cannot_create_a_second_continuation(setup):
    s = setup
    grant = enable(s).muse_grant_id
    exchange = MuseExchange(id="a" * 64, grant_id=grant, connection_id=s.whatsapp.connection().id,
                            account_id=s.whatsapp.connection().account_id, request_id="concurrent-request",
                            request_digest="b" * 64, status="starting")
    s.db.add(exchange)
    s.db.commit()
    result = {"exchange_id": exchange.id, "status": "pending", "sent_message_id": "sent-once"}
    with Session(s.db.get_bind()) as other:
        stale = other.get(MuseExchange, exchange.id)
        assert stale.agenda_item_id is None
        s.muse._record(exchange, result, wake=False)
        other_whatsapp = WhatsAppService(other, bridge=s.bridge, secret_store=FakeSecretStore())
        MuseService(other, other_whatsapp)._record(stale, result, wake=True)
    assert len(list(s.db.scalars(select(AssistantAgendaItem)))) == 1


def test_account_confirmation_must_match_both_status_and_current_connection(setup, monkeypatch):
    s = setup
    expected = s.whatsapp.connection().account_id
    s.whatsapp.connection().account_id = "other-account"
    s.db.commit()
    monkeypatch.setattr(s.whatsapp, "status", lambda: SimpleNamespace(connected=True, account_id=expected))
    with pytest.raises(WhatsAppError):
        s.whatsapp.configure_muse(enabled=True, account_id=expected)


@pytest.mark.parametrize("payload", [
    {"text": "Hello", "request_id": "request-first", "recipient": "+15551234567"},
    {"text": "Hello", "request_id": "short"},
    {"text": "Hello", "request_id": "request-first", "exchange_id": "a" * 64},
    {"exchange_id": "a" * 64, "wait_seconds": 21},
    {"exchange_id": "a" * 64, "standing_grant_id": 1},
])
def test_malformed_or_caller_authority_fields_never_dispatch(setup, payload):
    s = setup
    enable(s)
    with pytest.raises(IntegrationPolicyError):
        invoke(s, payload)
    assert not any(method == "muse_converse" for method, _ in s.bridge.calls)
