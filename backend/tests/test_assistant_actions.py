import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.execution.types import InvocationOutcome
from app.models import AgentProposal
from app.schemas.assistant_actions import FunctionStep
from app.services import assistant_action_service as runner
from app.services.agent_proposal_service import AgentProposalService
from app.services.assistant_actions import validate_action


def test_live_action_validation_rejects_unknown_calls_and_unresolved_arguments():
    catalog = {"test.write": {"input_schema": {"type": "object", "properties": {
        "title": {"type": "string"}}, "required": ["title"], "additionalProperties": False}}}
    for step in ({"function": "unknown", "arguments": {}},
                 {"function": "test.write", "arguments": {"title": "{{unresolved}}"}},
                 {"function": "test.write", "arguments": {"title": 3}}):
        with pytest.raises(ValueError):
            validate_action({"type": "functions", "description": "Do work", "steps": [step]}, catalog)
    assert FunctionStep(function="test.write", arguments={"title": "Resolved"}).function == "test.write"


def test_approved_sequence_stops_on_failure_retains_evidence_and_does_not_replay(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'actions.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    catalog = {name: {"category": "integration", "input_schema": {"type": "object"}}
               for name in ("first", "fails", "never")}
    monkeypatch.setattr(runner, "SessionLocal", factory)
    monkeypatch.setattr(runner, "deterministic_catalog", lambda *_: catalog)
    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_: None)
    monkeypatch.setattr(AgentProposalService, "_telegram", lambda *_a, **_kw: None)
    monkeypatch.setattr("app.services.assistant_activation_service.AssistantActivationService.tick", lambda *_: None)
    calls = []

    class Executor:
        def __init__(self, *_):
            pass

        def execute(self, target, _arguments, _context):
            calls.append(target.target_id)
            if target.target_id == "fails":
                return InvocationOutcome(status="failed", error_type="test_failure")
            return InvocationOutcome(status="succeeded", output={"verified": "first result"})

    monkeypatch.setattr(runner, "InvocationExecutor", Executor)
    with factory() as db:
        proposal = AgentProposal(source_session_id=1, title="Approved sequence", rationale="Reviewed", actions="Run",
            instruction="", fingerprint="sequence", status="approved", execution_status="queued",
            action_json={"type": "functions", "description": "Run", "steps": [
                {"function": name, "arguments": {}} for name in catalog]})
        db.add(proposal)
        db.commit()
        proposal_id = proposal.id
    runner.run_function_proposal(proposal_id)
    runner.run_function_proposal(proposal_id)
    assert calls == ["first", "fails"]
    with factory() as db:
        proposal = db.get(AgentProposal, proposal_id)
        assert proposal.execution_status == "failed"
        assert proposal.execution_state_json["completed_steps"] == 1
        assert "first result" in proposal.execution_state_json["results"][0]["output"]
    engine.dispose()
