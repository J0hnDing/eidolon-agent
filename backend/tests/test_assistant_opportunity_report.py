from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActSession, ActTurn, AgentProposal
from app.schemas.assistant_assessment import AssistantAssessmentResult
from app.services.agent_proposal_service import AgentProposalService
from app.services.assistant_assessment_result_service import AssistantAssessmentResultService


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def assessment(**changes):
    result = {
        "todo_doer": {"proposals": [{
            "todo_id": "todo-1",
            "action": {"type": "act", "description": "Prepare a draft", "instruction": "Draft it."},
        }]},
        "goal_doer": {"questions": [{
            "target": {"type": "goal", "id": "goal-1"}, "question": "Which direction?",
        }], "proposals": []},
        "opportunity_scout": {"opportunities": [{
            "title": "Research role", "description": "Current opening.", "proposals": [],
        }]},
    }
    result.update(changes)
    return result


def test_assessment_schema_forbids_extra_fields_and_todo_follow_up():
    payload = assessment()
    payload["todo_doer"]["proposals"][0]["extra"] = "no"
    with pytest.raises(ValidationError):
        AssistantAssessmentResult.model_validate(payload)
    payload = assessment()
    payload["todo_doer"]["proposals"][0]["follow_up"] = {
        "message": "Check back", "timing": {"type": "absolute", "at": "2026-10-01T12:00:00Z"},
    }
    with pytest.raises(ValidationError):
        AssistantAssessmentResult.model_validate(payload)


def test_result_writes_report_and_persists_proposal_and_question(db, monkeypatch):
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="thread-1")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Do an assessment now.", status="running")
    db.add(turn)
    db.commit()
    calls = []

    class FakeExecutor:
        def __init__(self, _db):
            pass

        def execute(self, target, values, context):
            calls.append((target, values, context))
            return SimpleNamespace(status="succeeded", output={"id": "report-1"})

    monkeypatch.setattr("app.services.assistant_assessment_result_service.InvocationExecutor", FakeExecutor)
    monkeypatch.setattr("app.services.assistant_assessment_result_service.resolve_target_name", lambda *_: "My goal")
    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_: None)
    monkeypatch.setattr(AgentProposalService, "_telegram", lambda *_args, **_kwargs: None)

    AssistantAssessmentResultService(db).accept(turn, AssistantAssessmentResult.model_validate(assessment()))

    db.refresh(turn)
    assert turn.assessment_report_id == "report-1"
    assert calls[0][0].target_id == "notion.report.create"
    assert calls[0][1]["select"] == "Opportunities"
    assert calls[0][1]["children"][0]["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "Research role — Current opening."
    proposal = db.scalar(select(AgentProposal))
    assert proposal.action_json["type"] == "act"
    assert proposal.provenance_json == {"kind": "todo", "todo_id": "todo-1"}
    question = db.scalar(select(ActTurn).where(ActTurn.backend_message_kind == "goal_question"))
    assert question.assistant_message == "Goal: My goal\nWhich direction?"
    assert question.codex_turn_id is None


def test_empty_opportunities_still_create_report(db, monkeypatch):
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="thread-empty")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Do an assessment now.", status="running")
    db.add(turn)
    db.commit()
    reports = []

    class FakeExecutor:
        def __init__(self, _db):
            pass

        def execute(self, _target, values, _context):
            reports.append(values)
            return SimpleNamespace(status="succeeded", output={"id": "empty-report"})

    monkeypatch.setattr("app.services.assistant_assessment_result_service.InvocationExecutor", FakeExecutor)
    payload = assessment(todo_doer={"proposals": []}, goal_doer={"questions": [], "proposals": []},
                         opportunity_scout={"opportunities": []})
    AssistantAssessmentResultService(db).accept(turn, AssistantAssessmentResult.model_validate(payload))
    assert reports[0]["children"] == []


def test_identical_proposals_in_one_result_are_suppressed(db, monkeypatch):
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="thread-duplicates")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Do an assessment now.", status="running")
    db.add(turn)
    db.commit()
    monkeypatch.setattr(AssistantAssessmentResultService, "_write_report", lambda *_: None)
    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_: None)
    monkeypatch.setattr(AgentProposalService, "_telegram", lambda *_args, **_kwargs: None)
    payload = assessment()
    payload["todo_doer"]["proposals"].append(payload["todo_doer"]["proposals"][0].copy())
    AssistantAssessmentResultService(db).accept(turn, AssistantAssessmentResult.model_validate(payload))
    assert len(list(db.scalars(select(AgentProposal)))) == 1
