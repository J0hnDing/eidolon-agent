from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.execution.context import InvocationContext
from app.execution.handlers.agent_private import AgentPrivateHandler
from app.execution.types import InvocationExecutionError, InvocationTargetRef
from app.models import ActSession, ActTurn
from app.services import act_turn_dispatcher, agent_policy_service
from app.services.act_turn_dispatcher import ActTurnDispatcher
from app.services.agent_policy_service import OPPORTUNITY_REPORT_TOOL_ID, AgentPolicyService
from app.services.assistant_assessment_service import AssistantAssessmentService
from app.services.mcp_function_service import McpFunctionError, McpFunctionService


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_assessment_writes_only_opportunity_list_without_approval(db, monkeypatch):
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="assistant-1")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Do an assessment now.", status="running")
    db.add(turn)
    db.commit()

    calls = []

    class FakeIntegrationInvocationService:
        def __init__(self, _db, *, compatibility_service):
            assert compatibility_service is not None

        def execute(self, context, operation_id, values):
            calls.append((context, operation_id, values))
            return SimpleNamespace(output={"id": "report-1"})

    monkeypatch.setattr(
        "app.execution.handlers.agent_private.build_default_integration_service",
        lambda _db: object(),
    )
    monkeypatch.setattr(
        "app.execution.handlers.agent_private.IntegrationInvocationService",
        FakeIntegrationInvocationService,
    )
    token = AgentPolicyService(db).issue("assistant", session.id)
    service = McpFunctionService(db, agent_token=token)
    name = service.tool_name("agent_private", OPPORTUNITY_REPORT_TOOL_ID)
    assert name in {tool.name for tool in service.list_tools()}
    assert name not in {tool.name for tool in McpFunctionService(db).list_tools()}
    assert AssistantAssessmentService(db).report_written(turn.id) is False

    result = service.invoke(name, {
        "opportunities": [
            {"name": "Research internship", "description": "A current opening aligned with the user's goal."},
        ],
    })
    assert result.output == {"report_id": "report-1"}
    assert AssistantAssessmentService(db).report_written(turn.id) is True
    assert len(calls) == 1
    context, operation_id, values = calls[0]
    assert context.principal_kind == "system"
    assert operation_id == "notion.report.create"
    assert values["select"] == "Opportunities"
    assert values["name"].startswith("Opportunity Scout — ")
    assert values["children"] == [{
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [
            {"type": "text", "text": {"content": "Research internship — A current opening aligned with the user's goal."}},
        ]},
    }]

    assert service.invoke(name, {"opportunities": []}).output == {"report_id": "report-1"}
    assert len(calls) == 1


@pytest.mark.parametrize("agent_id,origin,status", [
    ("act", "assessment", "running"),
    ("assistant", "web", "running"),
    ("assistant", "assessment", "succeeded"),
])
def test_report_rejects_other_agents_and_nonassessment_turns(db, agent_id, origin, status):
    session = ActSession(agent_id=agent_id, origin=origin, codex_thread_id=f"{agent_id}-{origin}")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="test", status=status)
    db.add(turn)
    db.commit()
    context = InvocationContext(
        principal_kind="agent", origin="agent_mcp", agent_id=agent_id,
        agent_session_id=session.id, agent_turn_id=turn.id,
    )
    with pytest.raises(InvocationExecutionError) as error:
        AgentPrivateHandler(db).execute(
            InvocationTargetRef(category="agent_private", target_id=OPPORTUNITY_REPORT_TOOL_ID),
            {"opportunities": []}, context,
        )
    assert error.value.error_type == "agent_permission_denied"


def test_report_input_is_bounded_and_assistant_policy_cannot_expose_it_to_act(db):
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="assistant-input")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="test", status="running")
    db.add(turn)
    db.commit()
    token = AgentPolicyService(db).issue("assistant", session.id)
    service = McpFunctionService(db, agent_token=token)
    name = service.tool_name("agent_private", OPPORTUNITY_REPORT_TOOL_ID)
    with pytest.raises(McpFunctionError) as error:
        service.invoke(name, {"opportunities": [{"name": "x", "description": "y", "extra": "z"}]})
    assert error.value.error_type == "invalid_input"
    assert AssistantAssessmentService(db).report_written(turn.id) is False
    assert agent_policy_service.PRIVATE_TOOL_AGENTS[OPPORTUNITY_REPORT_TOOL_ID] == "assistant"


def test_only_the_initial_assessment_turn_requires_a_written_report(db, monkeypatch):
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="pending:test")
    db.add(session)
    db.flush()
    first = ActTurn(session_id=session.id, user_message="Do an assessment now.", status="running")
    db.add(first)
    db.commit()

    class FakeSessions:
        def run_structured_turn(self, *_args, **_kwargs):
            return SimpleNamespace(output_text='{"response":"done"}', items=[])

    class FakeServer:
        sessions = FakeSessions()

        def start_thread(self, *_args, **_kwargs):
            return "assistant-thread"

        def resume_thread(self, *_args, **_kwargs):
            return None

    monkeypatch.setitem(
        act_turn_dispatcher.agent_app_servers,
        "assistant", FakeServer(),
    )
    monkeypatch.setattr(
        "app.services.act_turn_dispatcher.CodexRoutingService.resolve",
        lambda *_args, **_kwargs: SimpleNamespace(effective_model="test", effective_reasoning_effort="low"),
    )
    monkeypatch.setattr(
        "app.services.act_turn_dispatcher.TitleSyncService.sync_from_codex",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.agent_proposal_service.AgentProposalService.refresh_execution",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(AssistantAssessmentService, "notify_completed", lambda *_args: None)
    monkeypatch.setattr(
        "app.services.act_session_service.ActSessionService.prune_assistant_sessions",
        lambda *_args: None,
    )
    dispatcher = ActTurnDispatcher("assistant")
    dispatcher._execute(db, first.id)
    db.refresh(first)
    assert first.status == "failed"
    assert "opportunity scout report" in first.error_message

    followup = ActTurn(session_id=session.id, user_message="Follow up", status="running")
    db.add(followup)
    db.commit()
    dispatcher._execute(db, followup.id)
    db.refresh(followup)
    assert followup.status == "succeeded"
