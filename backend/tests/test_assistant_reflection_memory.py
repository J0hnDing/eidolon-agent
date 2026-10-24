"""Evening reflection is an ordinary recurring task using explicit memory tools."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    AssistantAgendaOccurrence,
)
from app.services.act_session_service import ActSessionService
from app.services.agent_memory_service import AgentMemoryService
from app.services.agent_policy_service import AgentPolicyService
from app.services.assistant_activation_service import AssistantActivationService
from app.services.assistant_service import AssistantService
from app.services.assistant_turn_dispatcher import AssistantTurnDispatcher
from app.services.function_catalog_service import FunctionCatalogService
from app.services.mcp_function_service import McpFunctionError, McpFunctionService

NOW = datetime(2026, 10, 18, 2, 5, tzinfo=UTC)  # October 17, 22:05 in Toronto.


@pytest.fixture
def db(tmp_path, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    for module in ("assistant_activation_service", "assistant_turn_dispatcher", "act_turn_dispatcher",
                   "agent_memory_service", "assistant_recurrence", "assistant_service"):
        monkeypatch.setattr(f"app.services.{module}.datetime", FrozenDatetime)
    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda *_a, **_k: [])
    monkeypatch.setattr("app.services.assistant_initialization.AssistantInitializationService.ready", lambda *_: False)
    from app.services.agent_dispatchers import agent_dispatchers

    monkeypatch.setattr(agent_dispatchers["assistant"], "notify", lambda: None)
    engine = create_engine(f"sqlite:///{tmp_path / 'reflection.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        service = AssistantService(session)
        service.state()
        for item in service.agenda():
            if item["title"] != "Evening reflection":
                service.delete_item(item["id"], item["revision"])
        yield session
    engine.dispose()


def reflection(db):
    item = next(item for item in AssistantService(db).agenda() if item["title"] == "Evening reflection")
    result = AssistantActivationService(db).run_now(item["id"], item["revision"])
    return db.get(ActTurn, result["turn_id"])


def memory_client(db, turn):
    turn.status = "running"
    db.commit()
    token = AgentPolicyService(db).issue("assistant", turn.session_id)
    return McpFunctionService(db, agent_token=token)


def invoke(client, target, payload):
    return client.invoke(client.tool_name("agent_private", target), payload).output


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_reflection_is_ordinary_recurring_agenda_and_claims_only_its_responsibility(db):
    primary = AssistantService(db).primary_session()
    unrelated = AssistantAgendaItem(title="Unrelated responsibility", due_at=NOW - timedelta(hours=1))
    proposal = AgentProposal(source_session_id=primary.id, title="Execution awaiting evaluation",
        rationale="Review", instruction="Review", actions="Review", fingerprint="reflection-outcome",
        execution_status="succeeded", execution_state_json={"completed_steps": 1})
    db.add_all([unrelated, proposal])
    db.commit()
    turn = reflection(db)
    activation = turn.assistant_context_json
    assert activation["kind"] == "agenda"
    assert activation["occurrence"]["timezone"] == "America/Toronto"
    assert activation["occurrence"]["scheduled_at"].startswith("2026-10-18T02:00:")
    assert [item["title"] for item in activation["agenda"]] == ["Evening reflection"]
    assert activation["executions"] == [] and not activation["initialize_atlas"]
    assert "assistant.memory.summary.write" in turn.user_message
    assert "reflection" not in activation and "summary_day" not in activation
    assert unrelated.last_turn_id is None and utc(unrelated.due_at) == NOW - timedelta(hours=1)
    assert proposal.execution_state_json == {"completed_steps": 1}


def test_explicit_summary_tool_saves_receipt_and_silent_success_completes_occurrence(db):
    turn = reflection(db)
    client = memory_client(db, turn)
    saved = invoke(client, "assistant.memory.summary.write", {
        "day": "2026-10-17", "summary": "Completed the application. Deferred the optional redesign."})
    assert saved["source_turn_id"] == turn.id
    assert saved["summary"].startswith("Completed the application.")
    assert AgentMemoryService(db).get_daily_summary("2026-10-17") == saved
    turn.status, turn.assistant_message, turn.completed_at = "succeeded", "", NOW
    db.commit()
    dispatcher = AssistantTurnDispatcher()
    dispatcher._finish_turn(db, turn)
    dispatcher._after_turn(db, turn)
    claimed = db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"])
    assert claimed.status == "succeeded"
    assert db.get(AssistantAgendaItem, claimed.agenda_item_id).status == "open"
    AgentMemoryService(db).sync_chat_history(now=NOW)
    assert AgentMemoryService(db).read_chat_history("2026-10-17", "America/Toronto")["messages"] == []


def test_final_response_does_not_implicitly_write_daily_summary(db):
    turn = reflection(db)
    turn.status, turn.assistant_message = "succeeded", "The agent must explicitly call the memory tool."
    AssistantTurnDispatcher()._finish_turn(db, turn)
    assert AgentMemoryService(db).get_daily_summary("2026-10-17") is None
    assert turn.assistant_message == "The agent must explicitly call the memory tool."


@pytest.mark.parametrize("summary", ["", " ", "x" * 1201])
def test_invalid_explicit_summary_fails_without_file_or_receipt(db, summary):
    turn = reflection(db)
    client = memory_client(db, turn)
    with pytest.raises(McpFunctionError):
        invoke(client, "assistant.memory.summary.write", {"day": "2026-10-17", "summary": summary})
    assert AgentMemoryService(db).get_daily_summary("2026-10-17") is None
    turn.status = "failed"
    AssistantActivationService(db).on_turn_completed(turn)
    claimed = db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"])
    assert claimed.status == "failed" and utc(claimed.retry_at) == NOW + timedelta(seconds=30)


def test_summary_tool_same_turn_receipt_is_idempotent_and_preserves_user_edits(db):
    turn = reflection(db)
    client = memory_client(db, turn)
    saved = invoke(client, "assistant.memory.summary.write", {
        "day": "2026-10-17", "summary": "Original recorded decision."})
    path = Path(saved["path"])
    path.write_text(path.read_text(encoding="utf-8").replace(
        "Original recorded decision.", "User corrected the decision."), encoding="utf-8")
    repeated = invoke(client, "assistant.memory.summary.write", {
        "day": "2026-10-17", "summary": "A retry must not overwrite user edits."})
    assert repeated["summary"] == "User corrected the decision."
    assert repeated["source_turn_id"] == turn.id


def test_startup_interrupts_incomplete_occurrence_without_rewriting_saved_summary(db, monkeypatch):
    turn = reflection(db)
    client = memory_client(db, turn)
    invoke(client, "assistant.memory.summary.write", {"day": "2026-10-17", "summary": "Made the recorded decision."})
    db.commit()
    monkeypatch.setattr("app.services.act_turn_dispatcher.SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(AgentMemoryService, "write_daily_summary", lambda *_a, **_k: pytest.fail("Duplicate write"))
    dispatcher = AssistantTurnDispatcher()
    assert dispatcher.recover() == 1
    db.expire_all()
    assert db.get(ActTurn, turn.id).status == "interrupted"
    claimed = db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"])
    assert claimed.status == "failed" and utc(claimed.retry_at) == NOW + timedelta(seconds=30)
    assert dispatcher.recover() == 0
    assert AgentMemoryService(db).get_daily_summary("2026-10-17")["summary"] == "Made the recorded decision."


def test_startup_reconciles_terminal_success_and_preserves_summary_user_edits(db, monkeypatch):
    turn = reflection(db)
    client = memory_client(db, turn)
    saved = invoke(client, "assistant.memory.summary.write", {
        "day": "2026-10-17", "summary": "Original recorded decision."})
    path = Path(saved["path"])
    path.write_text(path.read_text(encoding="utf-8").replace(
        "Original recorded decision.", "User corrected the decision and kept its receipt."), encoding="utf-8")
    edited = path.read_text(encoding="utf-8")
    turn.status, turn.assistant_message, turn.completed_at = "succeeded", "", NOW
    db.commit()  # Crash after terminal SQL commit, before the activation callback.
    monkeypatch.setattr("app.services.act_turn_dispatcher.SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(AgentMemoryService, "write_daily_summary", lambda *_a, **_k: pytest.fail("Duplicate write"))
    assert AssistantTurnDispatcher().recover() == 0
    db.expire_all()
    claimed = db.get(AssistantAgendaOccurrence, turn.assistant_context_json["occurrence"]["id"])
    assert claimed.status == "succeeded"
    assert AssistantActivationService(db).tick()["status"] == "idle"
    assert list(db.scalars(select(ActTurn.id))) == [turn.id]
    assert path.read_text(encoding="utf-8") == edited


def test_history_tool_reads_only_requested_local_days_full_three_agent_messages(db):
    expected = []
    ordinary_turn = None
    for agent in ("assistant", "act", "observer"):
        session = ActSession(agent_id=agent, codex_thread_id=f"thread-{agent}")
        db.add(session)
        db.flush()
        for day, stamp in (("previous", datetime(2026, 10, 17, 3, 59, tzinfo=UTC)),
                           ("today", datetime(2026, 10, 17, 4, 1, tzinfo=UTC)),
                           ("next", datetime(2026, 10, 18, 4, 1, tzinfo=UTC))):
            user, answer = f"{agent}-{day}-question", f"{agent}-{day}-answer"
            row = ActTurn(session_id=session.id, user_message=user, assistant_message=answer,
                          status="succeeded", created_at=stamp, completed_at=stamp)
            db.add(row)
            if day == "today":
                expected.extend((user, answer))
                if agent == "assistant":
                    ordinary_turn = row
    db.commit()
    turn = reflection(db)
    db.add(ActLiveMessage(turn_id=ordinary_turn.id, role="assistant", text="Live evidence", created_at=NOW))
    db.commit()
    client = memory_client(db, turn)
    context = invoke(client, "assistant.memory.history.read", {"day": "2026-10-17", "timezone": "America/Toronto"})
    assert sorted(item["text"] for item in context["messages"]) == sorted([*expected, "Live evidence"])
    assert {item["agent"] for item in context["messages"]} == {"assistant", "act", "observer"}
    assert not context["truncated"] and len(context["archive_directories"]) == 3
    assert len(list(db.scalars(select(ActTurn)))) == 10
    prompt = AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)
    assert "<reflection_context>" not in prompt
    data = json.loads(prompt.split("<assistant_context>")[1].split("</assistant_context>")[0])
    assert data["activation"]["kind"] == "agenda"
    assert "history" not in data["current"]


def test_reflection_uses_normal_authenticated_assistant_tools(db):
    turn = reflection(db)
    client = memory_client(db, turn)
    entries = invoke(client, "assistant.agenda.list", {})
    assert entries["items"][0]["title"] == "Evening reflection"
    created = invoke(client, "assistant.agenda.create", {"title": "Explicitly authorized ordinary task"})
    assert created["kind"] == "task"
    turn.cancel_requested_at = NOW
    db.commit()
    with pytest.raises(McpFunctionError, match="running turn|cancel"):
        invoke(client, "assistant.memory.summary.write", {"day": "2026-10-17", "summary": "Must not save."})
    assert AgentMemoryService(db).get_daily_summary("2026-10-17") is None


def test_memory_tools_are_assistant_only_and_unavailable_between_turns(db):
    turn = reflection(db)
    token = AgentPolicyService(db).issue("assistant", turn.session_id)
    client = McpFunctionService(db, agent_token=token)
    with pytest.raises(McpFunctionError, match="running turn"):
        invoke(client, "assistant.memory.summary.write", {"day": "2026-10-17", "summary": "Must not save."})
    public = McpFunctionService(db)
    assert public.tool_name("agent_private", "assistant.memory.summary.write") not in {tool.name for tool in public.list_tools()}
    other = ActSession(agent_id="act", codex_thread_id="other")
    db.add(other)
    db.commit()
    other_token = AgentPolicyService(db).issue("act", other.id)
    other_client = McpFunctionService(db, agent_token=other_token)
    assert other_client.tool_name("agent_private", "assistant.memory.summary.write") not in {
        tool.name for tool in other_client.list_tools()}


def test_reflection_browser_calls_use_normal_agent_policy(db, monkeypatch):
    from app import act_browser_mcp

    turn = reflection(db)
    turn.status = "running"
    db.commit()
    token = AgentPolicyService(db).issue("assistant", turn.session_id)
    monkeypatch.setattr(act_browser_mcp, "SessionLocal", sessionmaker(bind=db.get_bind()))
    requests = []

    def request(*args, **kwargs):
        requests.append((args, kwargs))
        return {"content": [], "isError": False}

    monkeypatch.setattr(act_browser_mcp, "browser_runtime_request", request)
    proxy = act_browser_mcp.ActBrowserMcpProxy("endpoint", "browser-token", str(turn.session_id), token)
    assert proxy.call_tool("browser_snapshot", {}).isError is False
    assert len(requests) == 1
    turn.cancel_requested_at = NOW
    db.commit()
    assert proxy.call_tool("browser_snapshot", {}).isError is True
    assert len(requests) == 1
