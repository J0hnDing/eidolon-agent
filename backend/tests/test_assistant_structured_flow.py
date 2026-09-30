from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ActSession, ActTurn, AgentProposal, AssistantFollowUp
from app.schemas.assistant_assessment import ASSESSMENT_RESPONSE_SCHEMA
from app.services import act_turn_dispatcher
from app.services.act_turn_dispatcher import ACT_RESPONSE_SCHEMA, ActTurnDispatcher
from app.services.agent_proposal_service import AgentProposalService
from app.services.assistant_action_service import process_due_followups, run_function_proposal
from app.services.assistant_assessment_result_service import validate_action
from app.services.telegram_service import TelegramService


@pytest.fixture
def database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        yield db, factory
    engine.dispose()


def test_only_first_origin_assessment_turn_uses_assessment_schema(database, monkeypatch):
    db, _factory = database
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="pending:test")
    db.add(session)
    db.flush()
    first = ActTurn(session_id=session.id, user_message="Do an assessment now.", status="running")
    db.add(first)
    db.commit()
    schemas = []

    class FakeSessions:
        def run_structured_turn(self, _thread, _input, schema, **_kwargs):
            schemas.append(schema)
            if len(schemas) == 1:
                return SimpleNamespace(output_text=(
                    '{"todo_doer":{"proposals":[]},"goal_doer":{"questions":[],"proposals":[]},'
                    '"opportunity_scout":{"opportunities":[]}}'
                ), items=[])
            return SimpleNamespace(output_text='{"response":"Thanks"}', items=[])

    class FakeServer:
        sessions = FakeSessions()

        def start_thread(self, *_args, **_kwargs):
            return "assistant-thread"

        def resume_thread(self, *_args, **_kwargs):
            return None

    monkeypatch.setitem(act_turn_dispatcher.agent_app_servers, "assistant", FakeServer())
    monkeypatch.setattr("app.services.act_turn_dispatcher.CodexRoutingService.resolve",
                        lambda *_args, **_kwargs: SimpleNamespace(
                            effective_model="test", effective_reasoning_effort="low"))
    monkeypatch.setattr("app.services.act_turn_dispatcher.TitleSyncService.sync_from_codex", lambda *_: None)
    monkeypatch.setattr("app.services.assistant_assessment_result_service.AssistantAssessmentResultService.accept",
                        lambda *_: None)
    monkeypatch.setattr("app.services.assistant_assessment_service.AssistantAssessmentService.notify_completed",
                        lambda *_: None)
    monkeypatch.setattr("app.services.act_session_service.ActSessionService.prune_assistant_sessions",
                        lambda *_: None)
    dispatcher = ActTurnDispatcher("assistant")
    dispatcher._execute(db, first.id)
    assert first.status == "succeeded"
    backend = ActTurn(session_id=session.id, user_message="", assistant_message="Goal: Research\nWhich area?",
                      backend_message_kind="goal_question", status="succeeded")
    db.add(backend)
    db.flush()
    second = ActTurn(session_id=session.id, user_message="Machine learning", status="running")
    db.add(second)
    db.commit()
    assert "Which area?" in dispatcher._turn_input(db, second, recovered_thread=False)
    assert "Which area?" in dispatcher._turn_input(db, second, recovered_thread=True)
    dispatcher._execute(db, second.id)
    assert second.status == "succeeded"
    assert schemas == [ASSESSMENT_RESPONSE_SCHEMA, ACT_RESPONSE_SCHEMA]


def test_live_function_validation_rejects_unresolved_or_unknown_calls():
    catalog = {"known": {"input_schema": {
        "type": "object", "properties": {"title": {"type": "string"}},
        "required": ["title"], "additionalProperties": False,
    }}}
    with pytest.raises(ValueError, match="unavailable"):
        validate_action({"type": "functions", "steps": [{"function": "missing", "arguments": {}}]}, catalog)
    with pytest.raises(ValueError, match="resolved"):
        validate_action({"type": "functions", "steps": [{
            "function": "known", "arguments": {"title": "{{from previous step}}"},
        }]}, catalog)


def test_pending_function_edits_change_fingerprint_and_revalidate(database, monkeypatch):
    db, _factory = database
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="thread-edit")
    db.add(session)
    db.flush()
    proposal = AgentProposal(
        source_session_id=session.id, title="Create event", rationale="", instruction="",
        actions="Create event", references_json=[], fingerprint="b" * 64,
        action_json={"type": "functions", "description": "Create event", "steps": [
            {"function": "known", "arguments": {"title": "Before"}},
        ]}, provenance_json={"kind": "opportunity", "title": "Event"},
    )
    db.add(proposal)
    db.commit()
    catalog = {"known": {"input_schema": {
        "type": "object", "properties": {"title": {"type": "string"}},
        "required": ["title"], "additionalProperties": False,
    }}}
    monkeypatch.setattr("app.services.assistant_assessment_result_service.deterministic_catalog",
                        lambda _db: catalog)
    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_: None)
    service = AgentProposalService(db)
    old_fingerprint = proposal.fingerprint
    service.edit_functions(proposal.id, [{"function": "known", "arguments": {"title": "After"}}])
    assert proposal.action_json["steps"][0]["arguments"]["title"] == "After"
    assert proposal.fingerprint != old_fingerprint
    with pytest.raises(ValueError, match="unavailable"):
        service.edit_functions(proposal.id, [{"function": "missing", "arguments": {}}])
    assert proposal.action_json["steps"][0]["arguments"]["title"] == "After"


def test_function_sequence_stops_on_failure_and_cancels_follow_up(database, monkeypatch):
    db, factory = database
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="thread")
    db.add(session)
    db.flush()
    action = {"type": "functions", "description": "Create two events", "steps": [
        {"function": "google_calendar.event.create", "arguments": {
            "title": "First", "end": {"date_time": "2026-10-01T10:00:00Z"},
        }},
        {"function": "google_calendar.event.create", "arguments": {
            "title": "Second", "end": {"date_time": "2026-10-02T10:00:00Z"},
        }},
        {"function": "google_calendar.event.create", "arguments": {
            "title": "Third", "end": {"date_time": "2026-10-03T10:00:00Z"},
        }},
    ]}
    proposal = AgentProposal(
        source_session_id=session.id, title=action["description"], rationale="", instruction="",
        actions=action["description"], references_json=[], fingerprint="f" * 64,
        action_json=action, status="approved", execution_status="queued",
    )
    db.add(proposal)
    db.flush()
    db.add(AssistantFollowUp(
        proposal_id=proposal.id, session_id=session.id, message="How was it?",
        timing_json={"type": "after_calendar_step", "step_index": 0, "delay_minutes": 30},
        status="waiting",
    ))
    db.commit()
    catalog = {"google_calendar.event.create": {
        "category": "integration", "input_schema": {
            "type": "object", "properties": {"title": {"type": "string"}, "end": {"type": "object"}},
            "required": ["title", "end"], "additionalProperties": False,
        },
    }}
    calls = []

    class FakeExecutor:
        def __init__(self, _db):
            pass

        def execute(self, _target, arguments, _context):
            calls.append(arguments["title"])
            return SimpleNamespace(
                status="succeeded" if len(calls) == 1 else "failed",
                output={"end": {"date_time": "2026-10-01T10:00:00Z"}},
            )

    monkeypatch.setattr("app.services.assistant_action_service.SessionLocal", factory)
    monkeypatch.setattr("app.services.assistant_action_service.deterministic_catalog", lambda _db: catalog)
    monkeypatch.setattr("app.services.assistant_action_service.InvocationExecutor", FakeExecutor)
    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_: None)
    monkeypatch.setattr(AgentProposalService, "_telegram", lambda *_: None)
    run_function_proposal(proposal.id)
    db.expire_all()
    assert calls == ["First", "Second"]
    assert db.get(AgentProposal, proposal.id).execution_status == "failed"
    assert db.scalar(select(AssistantFollowUp)).status == "cancelled"
    assert db.scalar(select(ActSession).where(ActSession.agent_id == "act")) is None


def test_calendar_follow_up_uses_executed_event_end(database, monkeypatch):
    db, factory = database
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="thread-calendar")
    db.add(session)
    db.flush()
    proposal = AgentProposal(
        source_session_id=session.id, title="Create event", rationale="", instruction="",
        actions="Create event", references_json=[], fingerprint="d" * 64,
        action_json={"type": "functions", "description": "Create event", "steps": [{
            "function": "google_calendar.event.create",
            "arguments": {"title": "Learning", "end": {"date_time": "2026-10-01T10:00:00Z"}},
        }]}, status="approved", execution_status="queued",
    )
    db.add(proposal)
    db.flush()
    db.add(AssistantFollowUp(
        proposal_id=proposal.id, session_id=session.id, message="Was it useful?",
        timing_json={"type": "after_calendar_step", "step_index": 0, "delay_minutes": 45},
        status="waiting",
    ))
    db.commit()

    class FakeExecutor:
        def __init__(self, _db):
            pass

        def execute(self, *_args):
            return SimpleNamespace(status="succeeded", output={
                "end": {"date_time": "2026-10-01T11:00:00Z"},
            })

    monkeypatch.setattr("app.services.assistant_action_service.SessionLocal", factory)
    monkeypatch.setattr("app.services.assistant_action_service.deterministic_catalog", lambda _db: {
        "google_calendar.event.create": {"category": "integration", "input_schema": {
            "type": "object", "properties": {"title": {"type": "string"}, "end": {"type": "object"}},
            "required": ["title", "end"], "additionalProperties": False,
        }},
    })
    monkeypatch.setattr("app.services.assistant_action_service.InvocationExecutor", FakeExecutor)
    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_: None)
    monkeypatch.setattr(AgentProposalService, "_telegram", lambda *_: None)
    run_function_proposal(proposal.id)
    db.expire_all()
    assert db.get(AgentProposal, proposal.id).execution_status == "succeeded"
    assert db.scalar(select(AssistantFollowUp)).due_at.replace(tzinfo=UTC) == datetime(
        2026, 10, 1, 11, 45, tzinfo=UTC,
    )


def test_due_follow_up_becomes_backend_assistant_message(database, monkeypatch):
    db, factory = database
    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="thread-follow-up")
    db.add(session)
    db.flush()
    proposal = AgentProposal(
        source_session_id=session.id, title="Action", rationale="", instruction="", actions="Action",
        references_json=[], fingerprint="a" * 64, status="approved", execution_status="succeeded",
    )
    db.add(proposal)
    db.flush()
    db.add(AssistantFollowUp(
        proposal_id=proposal.id, session_id=session.id, message="How did it go?",
        timing_json={"type": "absolute", "at": "2026-09-28T10:00:00Z"},
        due_at=datetime.now(UTC) - timedelta(minutes=1), status="scheduled",
    ))
    db.commit()
    monkeypatch.setattr("app.services.assistant_action_service.deliver_backend_message", lambda *_args, **_kwargs: None)
    process_due_followups(factory)
    db.expire_all()
    follow_up = db.scalar(select(AssistantFollowUp))
    message = db.scalar(select(ActTurn).where(ActTurn.backend_message_kind == "follow_up"))
    assert follow_up.status == "sent"
    assert message.assistant_message == "How did it go?"
    later = ActTurn(session_id=session.id, user_message="It went well", status="queued")
    db.add(later)
    db.commit()
    assert "How did it go?" in ActTurnDispatcher._turn_input(db, later, recovered_thread=False)


def test_telegram_proposal_labels_use_current_names_and_safe_fallback(database, monkeypatch):
    db, _factory = database
    proposal = AgentProposal(
        source_session_id=1, title="Description", rationale="", instruction="Sensitive Act detail",
        actions="Description", references_json=[], fingerprint="c" * 64,
        action_json={"type": "act", "description": "Prepare material", "instruction": "Sensitive Act detail"},
        provenance_json={"kind": "goal", "target": {"type": "subgoal", "id": "goal-1"},
                         "reason": "This advances the experiment."},
    )
    monkeypatch.setattr("app.services.assistant_assessment_result_service.resolve_target_name",
                        lambda *_: "Current research goal")
    service = TelegramService(db, role="assistant_agent")
    assert service._assessment_proposal_text(proposal) == (
        "Goal: Current research goal", "This advances the experiment.", "Prepare material",
    )
    monkeypatch.setattr("app.services.assistant_assessment_result_service.resolve_target_name",
                        lambda *_: "Goal")
    assert service._assessment_proposal_text(proposal)[0] == "Goal"


def test_existing_local_tables_gain_assessment_columns(tmp_path, monkeypatch):
    from app import db as database_module

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE agent_proposals (id INTEGER PRIMARY KEY, telegram_outcome_fingerprint VARCHAR(128))"))
        connection.execute(text("CREATE TABLE act_turns (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE assistant_follow_ups (id INTEGER PRIMARY KEY)"))
    monkeypatch.setattr(database_module, "engine", engine)
    database_module.ensure_local_schema()
    inspector = inspect(engine)
    proposals = {column["name"] for column in inspector.get_columns("agent_proposals")}
    turns = {column["name"] for column in inspector.get_columns("act_turns")}
    follow_ups = {column["name"] for column in inspector.get_columns("assistant_follow_ups")}
    assert {"action_json", "provenance_json", "follow_up_json", "execution_state_json"} <= proposals
    assert {"backend_message_kind", "assessment_report_id"} <= turns
    assert "sent_turn_id" in follow_ups
    engine.dispose()
