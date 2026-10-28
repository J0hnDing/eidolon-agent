from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.models import (
    ActLiveMessage,
    ActSession,
    ActTurn,
    AgentPolicy,
    AgentProposal,
    AssistantFollowUp,
    MemoryFact,
)
from app.routers.agents import router
from app.schemas.agents import AgentPolicyUpdate
from app.schemas.assistant import AgendaCreate, AgendaUpdate
from app.services.agent_memory_service import AgentMemoryService
from app.services.agent_proposal_service import AgentProposalService
from app.services.assistant_service import AssistantService
from app.services.function_catalog_service import FunctionCatalogService
from app.services.mcp_function_service import McpFunctionError, McpFunctionService


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda *_args, **_kw: [])
    monkeypatch.setattr("app.services.assistant_recurrence.migrate_schedules", lambda *_: None)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agenda_question_resolution_cas_and_session_independence(db):
    service = AssistantService(db)
    first = service.create_item(AgendaCreate(kind="question", title="Why research?",
        notes="User mentioned discovery and applications. Relative priority unknown.",
        due_at=datetime(2026, 10, 20, tzinfo=UTC)), session_id=99)
    assert service.create_item(AgendaCreate(kind="question", title="Why research?",
        notes="User mentioned discovery and applications. Relative priority unknown.",
        due_at=datetime(2026, 10, 20, tzinfo=UTC)))["id"] == first["id"]
    assert service.context(5)["agenda"][0]["kind"] == "question"
    changed = service.update_item(first["id"], AgendaUpdate(expected_revision=1, status="done",
        notes="Answered: discovery is the main motivation; evidence conversation 5."))
    assert changed["due_at"] is None
    assert changed["revision"] == 2
    assert service.agenda(active_only=True) == []
    with pytest.raises(ValueError, match="changed"):
        service.update_item(first["id"], AgendaUpdate(expected_revision=1, notes="Stale guess"))
    assert service.agenda()[0]["notes"].startswith("Answered")
    with pytest.raises(ValueError, match="changed"):
        service.delete_item(first["id"], 1)
    service.delete_item(first["id"], 2)
    assert service.agenda() == []


def test_daily_summary_survives_sessions_without_a_separate_continuity_store(db):
    service = AssistantService(db)
    memory = AgentMemoryService(db)
    memory.write_daily_summary("2026-10-17", "Meeting moved.", source_turn_id=10)
    context = service.context(100)
    assert "checkpoints" not in context
    assert context["change_tracking"]["pending_count"] == 0
    assert "summary" not in context
    assert context["memory"]["daily_summaries"][0]["summary"] == "Meeting moved."
    assert service.context(101)["memory"]["daily_summaries"] == context["memory"]["daily_summaries"]
    assert not hasattr(service, "update_context")


def test_context_never_copies_other_assistant_chat_transcripts(db):
    old = ActSession(agent_id="assistant", codex_thread_id="yesterday", status="archived")
    db.add(old)
    db.flush()
    db.add(ActTurn(session_id=old.id, user_message="OLD CHAT REQUEST",
                   assistant_message="OLD CHAT ANSWER", status="succeeded"))
    db.commit()
    context = AssistantService(db).context(999)
    assert "recent_other_conversations" not in context
    assert "OLD CHAT" not in str(context)


def test_context_keeps_responsibility_notes_and_compacts_recurring_definitions(db):
    service = AssistantService(db)
    task = service.create_item(AgendaCreate(title="Prepare document", notes="Evidence, next step and dependency."))
    long_task = service.create_item(AgendaCreate(title="Long responsibility", notes="x" * 1600))
    routine = service.create_item(AgendaCreate(title="Recurring routine", notes="EDITABLE_TASK_PROCEDURE",
        recurrence={"frequency": "daily", "time": "09:00", "timezone": "America/Toronto"}))
    context = service.context(100)
    entries = {entry["id"]: entry for entry in context["agenda"]}
    assert entries[task["id"]]["notes"] == task["notes"]
    assert entries[long_task["id"]]["notes_truncated"] is True
    assert entries[routine["id"]]["recurrence"] == routine["recurrence"]
    assert "notes" not in entries[routine["id"]]
    assert context["agenda_total"] == 3
    for entry in entries.values():
        assert {"id", "revision", "kind", "title", "status", "due_at"}.issubset(entry)
        assert not {"created_at", "updated_at", "source_session_id", "last_turn_id", "last_occurrence"} & entry.keys()
    # The full agenda API/tool contract retains all instructions and notes.
    assert next(entry for entry in service.agenda() if entry["id"] == routine["id"])["notes"] == routine["notes"]


def test_context_filters_evaluated_action_history_before_applying_limit(db):
    service = AssistantService(db)
    service.state()  # Finish one-time migration before constructing outcomes.

    def proposal(fingerprint, **values):
        row = AgentProposal(source_session_id=1, title=fingerprint, rationale="Reason", instruction="Instruction",
                            actions="Action", fingerprint=fingerprint, **values)
        db.add(row)
        db.flush()
        return row

    pending = proposal("old-pending", status="pending")
    queued = proposal("queued", status="approved", execution_status="queued")
    unreviewed = proposal("unreviewed", status="approved", execution_status="failed")
    for index in range(25):
        proposal(f"reviewed-{index}", status="approved", execution_status="succeeded",
                 execution_state_json={"assistant_outcome_turn_id": 42})
    proposal("denied", status="denied")
    db.commit()
    assert {entry["id"] for entry in service.context(1)["actions"]} == {pending.id, queued.id, unreviewed.id}


def test_prompt_context_retains_source_freshness_and_failure_without_poll_metadata(db, monkeypatch):
    from app.services.assistant_changes import AssistantChangeService

    monkeypatch.setattr(AssistantChangeService, "status", lambda *_: {"pending_count": 3, "sources": [{
        "source": "calendar.events:connection", "checked_at": "2026-10-20T12:00:00Z",
        "succeeded_at": "2026-10-19T12:00:00Z", "error": "connection_unavailable", "pending_count": 3}]})
    source = AssistantService(db).context(1)["change_tracking"]["sources"][0]
    assert source == {"source": "calendar.events:connection", "succeeded_at": "2026-10-19T12:00:00Z",
                      "error": "connection_unavailable", "pending_count": 3}


def test_web_turn_snapshots_assistant_telegram_pairing_without_automatic_delivery(db):
    from app.models import TelegramBotConnection
    from app.services.act_session_service import ActSessionService

    connection = TelegramBotConnection(role="assistant_agent", is_default=True,
        secret_store_id="fake", secret_reference="fake", bot_id="1", status="connected",
        paired_chat_id="123", paired_user_id="456")
    db.add(connection)
    db.commit()
    service = ActSessionService(db, agent_id="assistant")
    session = service.create_session()
    turn = service.enqueue_turn(session.id, "Send my report to Telegram")
    assert turn.delivery_connection_id == connection.id
    assert turn.delivery_chat_id == "123"
    assert turn.delivery_message_thread_id is None
    assert turn.delivery_provider is None
    assert turn.delivery_status is None


def test_persistent_assistant_credential_cannot_execute_tools_between_turns(db):
    from app.services.agent_policy_service import AgentPolicyService

    session = ActSession(agent_id="assistant", codex_thread_id="daily-thread")
    db.add(session)
    db.commit()
    token = AgentPolicyService(db).issue("assistant", session.id)
    mcp = McpFunctionService(db, agent_token=token)
    name = mcp.tool_name("agent_private", "assistant.agenda.create")
    assert name in {tool.name for tool in mcp.list_tools()}
    with pytest.raises(McpFunctionError, match="requires a running turn"):
        mcp.invoke(name, {"title": "Must not run while idle"})
    turn = ActTurn(session_id=session.id, user_message="Work", status="running")
    db.add(turn)
    db.commit()
    assert mcp.invoke(name, {"title": "Authorized current work"}).output["last_turn_id"] == turn.id
    turn.cancel_requested_at = datetime.now(UTC)
    db.commit()
    with pytest.raises(McpFunctionError, match="requires a running turn"):
        mcp.invoke(name, {"title": "Must not run after cancellation"})


def test_memory_attribution_and_correction_preserve_unknown_and_prevent_stale_overwrite(db):
    service = AssistantService(db)
    hypothesis = service.remember({"key": "planning_style", "value": "May prefer a short plan",
        "category": "preferences", "attribution": "inferred", "evidence": "Asked twice for shorter plans"})
    assert hypothesis["provenance_json"]["attribution"] == "inferred"
    assert hypothesis["category"] == "facts"
    correction = service.remember({"id": hypothesis["id"], "expected_value": hypothesis["value"],
        "key": "planning_style", "value": "Wants detailed plans for unfamiliar tasks",
        "category": "preferences", "attribution": "stated", "evidence": "Explicit correction"})
    assert correction["provenance_json"]["attribution"] == "stated"
    with pytest.raises(ValueError, match="changed"):
        service.remember({"id": hypothesis["id"], "expected_value": hypothesis["value"],
            "key": "planning_style", "value": "Short", "category": "preferences",
            "attribution": "inferred", "evidence": "Old observation"})
    context = service.context(3)
    memory = AgentMemoryService(db)
    assert memory.list_facts()[0]["value"] == correction["value"]
    assert context["memory"]["facts_directory"] == "memory/facts-outside-atlas/"
    assert "Wants detailed plans" not in str(context)
    memory.delete_fact(hypothesis["id"])
    assert memory.list_facts() == []
    assert list(db.scalars(select(MemoryFact))) == []


def test_old_followups_migrate_once_and_old_policy_gains_act_permissions(db):
    db.add(AgentPolicy(id="assistant", policy_json={**AgentPolicyUpdate().model_dump(), "banned_functions": ["blocked"]}))
    proposal = AgentProposal(source_session_id=4, title="Prepare", rationale="Useful", actions="Draft",
        instruction="Draft", fingerprint="historical", execution_status="succeeded")
    db.add(proposal)
    db.flush()
    db.add(AssistantFollowUp(proposal_id=proposal.id, session_id=4, message="How did it go?",
        timing_json={"type": "absolute", "at": "2026-10-20T10:00:00Z"}, status="scheduled",
        due_at=datetime(2026, 10, 20, 10, tzinfo=UTC)))
    db.commit()
    service = AssistantService(db)
    service.state()
    service.state()
    assert len(service.agenda()) == 1
    assert service.agenda()[0]["kind"] == "question"
    assert db.scalar(select(AssistantFollowUp)).status == "migrated"
    assert db.get(AgentPolicy, "assistant").policy_json["read_only"] is False
    assert db.get(AgentPolicy, "assistant").policy_json["banned_functions"] == ["blocked"]
    assert proposal.execution_state_json["assistant_outcome_turn_id"] == 0


@pytest.mark.parametrize("legacy_follow_up", [None, {}, [], {"message": None}, {"message": ""}])
def test_initialization_skips_null_or_malformed_legacy_followups(db, legacy_follow_up):
    db.add(AgentProposal(source_session_id=4, title="Legacy action", rationale="Requested",
        actions="Draft", instruction="Draft", fingerprint="legacy-null", status="pending",
        follow_up_json=legacy_follow_up))
    db.commit()
    service = AssistantService(db)
    assert service.state().id == 1
    assert service.agenda() == []
    assert service.state().id == 1


def test_private_tools_use_real_authenticated_invocation_path_and_not_public_or_observer(db):
    from app.services.agent_policy_service import AgentPolicyService

    service = AssistantService(db)
    service.state()
    session = ActSession(agent_id="assistant", codex_thread_id="pending:assistant")
    db.add(session)
    db.flush()
    db.add(ActTurn(session_id=session.id, user_message="Help", status="running", activity_json=[]))
    db.commit()
    token = AgentPolicyService(db).issue("assistant", session.id)
    mcp = McpFunctionService(db, agent_token=token)
    name = mcp.tool_name("agent_private", "assistant.agenda.create")
    result = mcp.invoke(name, {"title": "Prepare draft", "kind": "task"})
    assert result.output["source_session_id"] == session.id
    assert result.output["last_turn_id"] is not None
    context_tool = mcp.tool_name("agent_private", "assistant.context.update")
    assert context_tool not in {tool.name for tool in mcp.list_tools()}
    with pytest.raises(McpFunctionError):
        mcp.invoke(context_tool, {"source": "calendar", "cursor": "skip-all"})
    with pytest.raises(McpFunctionError):
        mcp.invoke(context_tool, {"summary": "Prepare draft"})
    assert name not in {tool.name for tool in McpFunctionService(db).list_tools()}
    observer = ActSession(agent_id="observer", codex_thread_id="pending:observer")
    db.add(observer)
    db.commit()
    observer_mcp = McpFunctionService(db, agent_token=AgentPolicyService(db).issue("observer", observer.id))
    assert name not in {tool.name for tool in observer_mcp.list_tools()}
    with pytest.raises(McpFunctionError, match="not in"):
        observer_mcp.invoke(name, {"title": "Overwrite"})
    AgentPolicyService(db).update("assistant", AgentPolicyUpdate(read_only=False,
        banned_functions=["assistant.agenda.create"]))
    with pytest.raises(McpFunctionError, match="banned"):
        mcp.invoke(name, {"title": "Blocked"})


def test_daily_plan_tool_uses_authenticated_catalog_and_saved_agenda_ids(db):
    from datetime import timedelta

    from app.services.agent_policy_service import AgentPolicyService

    service = AssistantService(db)
    service.state()
    session = ActSession(agent_id="assistant", codex_thread_id="pending:planning")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Plan today", status="running")
    db.add(turn)
    db.commit()
    now = datetime.now(UTC)
    selection = {"work": [], "interaction": [], "other": []}
    for category, count in (("work", 2), ("interaction", 2), ("other", 1)):
        for index in range(count):
            item = service.create_item(AgendaCreate(
                title=f"{category} {index}", due_at=now + timedelta(seconds=1)))
            selection[category].append(item["id"])
    mcp = McpFunctionService(db, agent_token=AgentPolicyService(db).issue("assistant", session.id))
    name = mcp.tool_name("agent_private", "assistant.plan.save")
    result = mcp.invoke(name, selection)
    assert result.output["planned"] == {"work": 2, "interaction": 2, "other": 1}
    assert result.output["completed"] == {"work": 0, "interaction": 0, "other": 0}
    assert result.output["planned_satisfied"] is True
    assert name not in {tool.name for tool in McpFunctionService(db).list_tools()}
    db.refresh(turn)
    assert turn.assistant_context_json["daily_plan"]["items"]["work"][0]["id"] == selection["work"][0]
    # Saving plan metadata must not hide an ordinary user message from history.
    turn.status, turn.completed_at, turn.assistant_message = "succeeded", now, "Plan saved."
    db.commit()
    AgentMemoryService(db).sync_chat_history(now=now)
    assert AgentMemoryService(db).read_turn_content(turn)["user_message"] == "Plan today"


def test_approval_is_durable_deduplicated_and_does_not_execute_without_decision(db, monkeypatch):
    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_: None)
    monkeypatch.setattr(AgentProposalService, "_telegram", lambda *_a, **_kw: None)
    service = AssistantService(db)
    request = {"description": "Submit prepared application", "reason": "External submission",
        "instruction": "Submit only the reviewed final file at workspace/application.pdf."}
    result = service.request_approval(request, 1)
    assert result["status"] == "pending"
    assert result["execution_status"] is None
    assert service.request_approval(request, 1)["proposal_id"] == result["proposal_id"]
    proposal = AgentProposalService(db).decide(result["proposal_id"], False)
    assert proposal.status == "denied"
    assert proposal.act_session_id is None


def test_agenda_api_validation_and_edit_conflicts(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        assert client.post("/agents/assistant/agenda", json={"title": "Check", "due_at": "2026-10-20T09:00:00"}).status_code == 422
        created = client.post("/agents/assistant/agenda", json={"title": "Check", "kind": "question"}).json()
        assert created["question_category"] == "common"
        assert client.post("/agents/assistant/agenda", json={
            "title": "Invalid", "kind": "question", "question_category": "urgent",
        }).status_code == 422
        path = f"/agents/assistant/agenda/{created['id']}"
        changed = client.patch(path, json={
            "expected_revision": 1, "notes": "Evidence", "question_category": "temporal",
            "due_at": "2026-10-20T07:30:00Z",
        })
        assert changed.status_code == 200
        assert changed.json()["question_category"] == "temporal"
        assert changed.json()["due_at"] == "2026-10-20T07:30:00+00:00"
        assert client.patch(path, json={"expected_revision": 1, "notes": "Stale"}).status_code == 409
        assert client.put("/agents/assistant/runtime", json={"timezone": "invalid/zone"}).status_code == 422
        assert client.put("/agents/assistant/runtime", json={"daily_time": "25:00"}).status_code == 422
        assert client.put("/agents/assistant/runtime", json={"enabled": None}).status_code == 422
        assert client.put("/agents/assistant/runtime", json={"summary": "Useful continuity"}).status_code == 422
        assert client.put("/agents/assistant/runtime", json={"reflection_enabled": False}).status_code == 422
        runtime = client.get("/agents/assistant/runtime").json()
        assert "summary" not in runtime
        assert "reflection_enabled" not in runtime
        assert "daily_time" not in runtime
        assert client.post("/agents/assistant/runtime/run", json={"kind": "reflection"}).status_code == 404
        assert client.get("/agents/assistant/assessment").status_code == 404
        assert client.delete(path + "?expected_revision=2").status_code == 204


def test_refresh_api_keeps_the_permanent_chat_and_preserves_state(db):
    memory = AgentMemoryService(db)
    saved = memory.write_daily_summary("2026-10-17", "Made an important decision.", source_turn_id=22)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        first = client.post("/agents/assistant/sessions", json={}).json()
        second = client.post("/agents/assistant/sessions", json={"origin": "web"}).json()
        assert first["id"] == second["id"]
        result = client.post("/agents/assistant/runtime/refresh")
        assert result.status_code == 200
        assert result.json()["status"] == "refresh_pending"
        assert result.json()["primary_session_id"] == first["id"]
        assert result.json()["refresh_pending"] is True
        assert "summary" not in result.json()
        assert memory.get_daily_summary("2026-10-17") == saved
        assert client.delete(f"/agents/assistant/sessions/{first['id']}").status_code == 409


def test_recurring_agenda_api_run_history_and_pause_use_shared_records(db, monkeypatch):
    from app.services.act_session_service import ActSessionService
    from app.services.assistant_activation_service import AssistantActivationService

    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        created = client.post("/agents/assistant/agenda", json={"title": "Weekly planning",
            "recurrence": {"frequency": "weekly", "time": "09:00", "timezone": "America/Toronto",
                           "weekdays": [0, 4]}})
        assert created.status_code == 201
        item = created.json()
        path = f"/agents/assistant/agenda/{item['id']}"
        assert client.post(path + "/run", json={"expected_revision": 99}).status_code == 409
        response = client.post(path + "/run", json={"expected_revision": item["revision"]})
        assert response.status_code == 200 and response.json()["kind"] == "agenda"
        turn = db.get(ActTurn, response.json()["turn_id"])
        turn.status = "succeeded"
        db.commit()
        AssistantActivationService(db).on_turn_completed(turn)
        history = client.get(path + "/occurrences").json()
        assert len(history) == 1 and history[0]["status"] == "succeeded"
        assert history[0]["turn_id"] == turn.id
        paused = client.patch(path, json={"expected_revision": item["revision"], "status": "waiting"}).json()
        assert paused["due_at"] is None
        assert client.post(path + "/run", json={"expected_revision": paused["revision"]}).status_code == 409
        assert client.patch(path, json={"expected_revision": paused["revision"], "kind": "question"}).status_code == 409


@pytest.mark.parametrize("approval_source", ["ordinary", "ordinary_with_plan", "live_reply"])
def test_natural_approval_requires_current_user_evidence_and_is_consumed_once(db, monkeypatch, approval_source):
    from app.services.act_session_service import ActSessionService
    from app.services.act_turn_dispatcher import act_turn_dispatcher

    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_: None)
    monkeypatch.setattr(AgentProposalService, "_telegram", lambda *_a, **_kw: None)
    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    monkeypatch.setattr(act_turn_dispatcher, "notify", lambda: None)
    session = ActSession(agent_id="assistant", codex_thread_id="assistant-approval")
    db.add(session)
    db.commit()
    service = AssistantService(db)
    result = service.request_approval({"description": "Send reviewed document", "reason": "External contact",
        "instruction": "Send only the reviewed document to the specified recipient."}, session.id)
    turn = ActTurn(session_id=session.id, user_message="Yes, send the reviewed document.", status="running",
                   assistant_context_json={"kind": "daily"})
    db.add(turn)
    db.commit()
    payload = {"proposal_id": result["proposal_id"], "approve": True, "user_statement": "Yes, send the reviewed document."}
    with pytest.raises(ValueError, match="current user"):
        service.decide_approval(payload, session.id, turn.id)
    if approval_source in {"ordinary", "ordinary_with_plan"}:
        turn.assistant_context_json = {"daily_plan": {"day": "2026-10-25"}} if approval_source == "ordinary_with_plan" else None
    else:
        prompt = ActLiveMessage(turn_id=turn.id, role="assistant", text="Approve this action?",
                               reply_status="answered")
        db.add(prompt)
        db.flush()
        db.add(ActLiveMessage(turn_id=turn.id, role="user", text=payload["user_statement"],
                             reply_to_message_id=prompt.id, reply_status="none"))
    db.commit()
    with pytest.raises(ValueError, match="quote"):
        service.decide_approval({**payload, "user_statement": "Invented consent"}, session.id, turn.id)
    approved = service.decide_approval(payload, session.id, turn.id)
    assert approved["status"] == "approved"
    proposal = db.get(AgentProposal, result["proposal_id"])
    assert proposal.act_session_id is not None
    assert proposal.execution_state_json["user_decision"]["turn_id"] == turn.id
    with pytest.raises(ValueError, match="already"):
        service.decide_approval(payload, session.id, turn.id)
    assert len(list(db.scalars(select(ActSession).where(ActSession.agent_id == "act")))) == 1


def test_repeated_request_after_decision_gets_a_new_approval_but_pending_and_same_turn_deduplicate(db, monkeypatch):
    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_: None)
    monkeypatch.setattr(AgentProposalService, "_telegram", lambda *_a, **_kw: None)
    service = AssistantService(db)
    request = {"description": "Send report", "reason": "External contact", "instruction": "Send the reviewed report."}
    first = service.request_approval(request, 1, 10)
    assert service.request_approval(request, 1, 11)["proposal_id"] == first["proposal_id"]
    AgentProposalService(db).decide(first["proposal_id"], False)
    assert service.request_approval(request, 1, 10)["proposal_id"] == first["proposal_id"]
    second = service.request_approval(request, 1, 12)
    assert second["proposal_id"] != first["proposal_id"]
    assert second["status"] == "pending"


def test_migration_retains_relative_timing_and_newly_interrupted_outcomes(db):
    first = AgentProposal(source_session_id=1, title="Pending action", rationale="", actions="Do it",
        instruction="Do it", fingerprint="pending", status="pending", follow_up_json={
            "message": "How was the meeting?", "timing": {"type": "after_calendar_step", "step_index": 0, "delay_minutes": 60}})
    old = AgentProposal(source_session_id=1, title="Past action", rationale="", actions="Done",
        instruction="Done", fingerprint="past", status="approved", execution_status="succeeded",
        follow_up_json={"message": "Already asked", "timing": {"type": "absolute", "at": "2026-10-15T10:00:00Z"}})
    interrupted = AgentProposal(source_session_id=1, title="Interrupted action", rationale="", actions="Check",
        instruction="Check", fingerprint="interrupted", status="approved", execution_status="interrupted")
    db.add_all([first, old, interrupted])
    db.flush()
    db.add(AssistantFollowUp(proposal_id=old.id, session_id=1, message="Already asked", timing_json={}, status="delivered"))
    db.commit()
    service = AssistantService(db)
    service.state()
    items = service.agenda()
    assert len(items) == 1
    assert "after_calendar_step" in items[0]["notes"]
    assert str(first.id) in items[0]["notes"]
    assert interrupted.execution_state_json is None


def test_catalog_probes_each_provider_once_and_keeps_notion_resource_groups_separate(db, monkeypatch):
    from app.services import function_catalog_service as catalog_module
    from app.services.invocation_approval_service import InvocationApprovalService

    calls = []

    class Integrations:
        def operation_available(self, operation_id):
            calls.append(operation_id)
            return not operation_id.startswith("notion.daily_feed.")

    monkeypatch.setattr(catalog_module, "build_default_integration_service", lambda *_: Integrations())
    monkeypatch.setattr(catalog_module, "codex_available", lambda: False)
    monkeypatch.setattr(InvocationApprovalService, "approval_available", lambda *_: False)
    entries = FunctionCatalogService(db)._fixed_entries()
    assert len([op for op in calls if op.startswith("atlas.")]) == 1
    assert len([op for op in calls if op.startswith("notion.")]) == 3
    by_id = {entry["id"]: entry for entry in entries}
    assert by_id["notion.daily_feed.read"]["availability"] == "unavailable"
    assert by_id["notion.daily_feed.write"]["availability"] == "unavailable"
    assert by_id["notion.todo.list"]["availability"] == "available"


def test_schema_upgrade_preserves_existing_memories_and_is_idempotent(tmp_path, monkeypatch):
    from sqlalchemy import inspect, text

    from app import db as db_module
    from app.models import AssistantState

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        memory = MemoryFact(key="writing_style", value="Keep explanations direct", category="writing_style")
        session.add(memory)
        session.add(AssistantState(id=1, summary="Keep continuity"))
        session.commit()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE memory_facts DROP COLUMN provenance_json"))
        connection.execute(text("ALTER TABLE act_turns DROP COLUMN assistant_context_json"))
        connection.execute(text("ALTER TABLE act_turns DROP COLUMN assistant_review_json"))
        connection.execute(text("ALTER TABLE assistant_state ADD COLUMN checkpoints_json JSON"))
        connection.execute(text("UPDATE assistant_state SET checkpoints_json = '{\"mail\": \"untrusted-cursor\"}'"))
    monkeypatch.setattr(db_module, "engine", engine)
    db_module.ensure_local_schema()
    db_module.ensure_local_schema()
    assert "checkpoints_json" not in {column["name"] for column in inspect(engine).get_columns("assistant_state")}
    with Session(engine) as session:
        memory = session.scalar(select(MemoryFact))
        assert memory.value == "Keep explanations direct"
        assert memory.provenance_json is None
        assert session.get(AssistantState, 1).summary == "Keep continuity"
        conversation = ActSession(agent_id="assistant", codex_thread_id="upgraded-conversation")
        session.add(conversation)
        session.flush()
        turn = ActTurn(session_id=conversation.id, user_message="Hello", assistant_context_json={"kind": "daily"})
        session.add(turn)
        session.commit()
        assert session.get(ActTurn, turn.id).assistant_context_json == {"kind": "daily"}
        assert session.get(ActTurn, turn.id).assistant_review_json is None
    engine.dispose()
